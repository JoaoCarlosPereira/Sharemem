/*!
 * Mem0 Shared — fail-closed auth bridge for PLANKA `/api/*`.
 *
 * When AUTH_JWT_SECRET is set, every `/api/*` request must present one of:
 *   - HS256 JWT signed with AUTH_JWT_SECRET (same secret as Mem0 / NEXTAUTH_SECRET)
 *   - Authorization: Bearer local  (only if MEM0_AUTH_ALLOW_LEGACY=1)
 *   - INTERNAL_ACCESS_TOKEN (existing PLANKA internal bearer)
 *   - omtk_* agent token (lookup in public.agent_tokens on the shared Postgres)
 *
 * Bearer INTERNAL / legacy / omtk → DEFAULT_ADMIN (FK-safe mirror actor).
 * JWT de sessão UI → upsert user_account por e-mail (nome/foto/pt-BR) e
 * req.currentUser nesse usuário (ADR-008). Falha no upsert JWT → 401
 * (nunca DEFAULT_ADMIN para embed UI).
 *
 * When AUTH_JWT_SECRET is empty, the bridge is a no-op (upstream PLANKA auth only).
 */

const { Client } = require('pg');

const { authenticateMem0Request, authenticateOmtk } = require('./lib/validate-auth');
const {
  ensureSharedAccess: ensureSharedAccessForGroup,
} = require('../../../utils/mem0-shared-access');

module.exports = function defineMem0AuthHook(sails) {
  let pgClient = null;
  let pgClientFailed = false;
  let cachedAdminUser = null;
  let cachedAdminAt = 0;
  /** userId → last ensure timestamp (ms); re-sync to pick up boards novos. */
  const membershipEnsuredAt = new Map();
  const MEMBERSHIP_TTL_MS = 30000;

  const getPgClient = async () => {
    if (pgClient) return pgClient;
    if (pgClientFailed) return null;
    const dsn = String(process.env.DATABASE_URL || '').trim();
    if (!dsn) {
      pgClientFailed = true;
      return null;
    }
    try {
      const client = new Client({ connectionString: dsn });
      await client.connect();
      pgClient = client;
      return pgClient;
    } catch (err) {
      pgClientFailed = true;
      sails.log.warn('mem0-auth: could not open Postgres for omtk_ lookup:', err.message);
    }
    return null;
  };

  const resolveServiceUser = async () => {
    const now = Date.now();
    if (cachedAdminUser && now - cachedAdminAt < 60000) {
      return cachedAdminUser;
    }
    const email = String(process.env.DEFAULT_ADMIN_EMAIL || '')
      .trim()
      .toLowerCase();
    if (!email || typeof User === 'undefined' || !User.qm) {
      return null;
    }
    try {
      const user = await User.qm.getOneByEmail(email);
      if (user && !user.isDeactivated) {
        cachedAdminUser = user;
        cachedAdminAt = now;
        return user;
      }
    } catch (err) {
      sails.log.warn('mem0-auth: failed to resolve DEFAULT_ADMIN user:', err.message);
    }
    return null;
  };

  const normalizeEmail = (raw, fallbackSub) => {
    const email = String(raw || '')
      .trim()
      .toLowerCase();
    if (email && email.includes('@')) return email;
    const sub = String(fallbackSub || 'mem0-user')
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9._+-]/g, '-');
    return `${sub || 'mem0-user'}@mem0.local`;
  };

  const ensureSharedAccess = async (user, userGroupId) => {
    if (!user || !user.id || !userGroupId) return;
    if (typeof Project === 'undefined' || !Project.qm || typeof ProjectManager === 'undefined') {
      return;
    }
    if (typeof Board === 'undefined' || !Board.qm || typeof BoardMembership === 'undefined') {
      return;
    }
    const now = Date.now();
    const last = membershipEnsuredAt.get(user.id) || 0;
    if (now - last < MEMBERSHIP_TTL_MS) return;

    try {
      const outcome = await ensureSharedAccessForGroup({
        user,
        userGroupId,
        models: {
          Project,
          Board,
          BoardMembership,
          ProjectManager,
          // Limpeza da revogação (inscrições/cards/tasks), como em board-memberships/delete-one.
          BoardSubscription,
          Card,
          CardSubscription,
          CardMembership,
          TaskList,
          Task,
        },
        runQuery: (sql, values) => sails.sendNativeQuery(sql, values),
        internalAdminEmail: process.env.DEFAULT_ADMIN_EMAIL,
        internalUserId: typeof User !== 'undefined' && User.INTERNAL ? User.INTERNAL.id : null,
        sockets: sails.sockets,
      });
      if (outcome.errors.length > 0) {
        sails.log.warn(
          `mem0-auth: shared access partially reconciled for user ${user.id}:`,
          outcome.errors.join('; '),
        );
      }
      // Memoriza mesmo com falha parcial (reconciliação é cara); o TTL curto
      // garante nova tentativa em ~30s.
      membershipEnsuredAt.set(user.id, now);
    } catch (err) {
      sails.log.warn('mem0-auth: failed to ensure shared access:', err.message);
    }
  };

  const upsertJwtUser = async (auth) => {
    if (typeof User === 'undefined' || !User.qm) {
      return null;
    }

    const email = normalizeEmail(auth.email, auth.subject);
    const name = String(auth.name || auth.subject || email.split('@')[0] || 'Mem0 User').trim();
    const picture = String(auth.picture || '').trim();
    const actorUser = User.INTERNAL || (await resolveServiceUser());

    let user;
    try {
      user = await sails.helpers.mem0.upsertUserByEmail.with({
        email,
        name,
        picture: picture || null,
        actorUser: actorUser || (await resolveServiceUser()),
      });
    } catch (err) {
      sails.log.warn('mem0-auth: failed to upsert JWT user:', err.message || err);
      return null;
    }

    let userGroupId = auth.group;
    if (!userGroupId) {
      try {
        const client = await getPgClient();
        if (client) {
          const res = await client.query(
            'SELECT group_id FROM public.users WHERE lower(email) = lower($1) LIMIT 1',
            [email],
          );
          if (res.rows.length > 0) {
            userGroupId = res.rows[0].group_id;
          }
        }
      } catch (err) {
        sails.log.warn('mem0-auth: failed to look up user group:', err.message);
      }
    }

    if (!userGroupId) {
      // Fail closed: without a resolvable group the request is denied.
      sails.log.warn('mem0-auth: user has no group, skipping access grant', email);
      return null;
    }

    await ensureSharedAccess(user, userGroupId);
    return user;
  };

  return {
    async initialize() {
      sails.log.info('Initializing custom hook (`mem0-auth`)');
    },

    routes: {
      before: {
        '/api/*': {
          async fn(req, res, next) {
            const result = authenticateMem0Request({
              authorizationHeader: req.headers && req.headers.authorization,
              path: req.path,
              env: process.env,
            });

            if (result.method === 'disabled') {
              return next();
            }

            let auth = result;
            if (result.needsOmtkLookup) {
              const client = await getPgClient();
              auth = await authenticateOmtk(result.token, client);
            }

            if (!auth.ok) {
              return res.status(401).json({
                code: 'E_MEM0_UNAUTHORIZED',
                message: 'Mem0 authentication required',
                reason: auth.reason || 'unauthorized',
              });
            }

            req.mem0Auth = {
              method: auth.method,
              subject: auth.subject,
              email: auth.email,
              name: auth.name,
              picture: auth.picture,
              group: auth.group,
            };

            if (auth.method === 'jwt') {
              // UI embed: sempre a pessoa do JWT — nunca DEFAULT_ADMIN.
              const jwtUser = await upsertJwtUser(auth);
              if (!jwtUser) {
                return res.status(401).json({
                  code: 'E_MEM0_USER_UPSERT',
                  message: 'Could not resolve Mem0 user for JWT embed',
                });
              }
              req.currentUser = jwtUser;
              if (jwtUser.language && typeof req.setLocale === 'function') {
                req.setLocale(jwtUser.language);
              }
              // Igual à sessão nativa (current-user): sem a sala `@user:<id>`,
              // removeRoomMembersFromRooms não tira o socket do embed de
              // `board:<id>` quando a membership é revogada.
              if (req.isSocket) {
                sails.sockets.join(req, `@user:${jwtUser.id}`);
              }
              return next();
            }

            // Mirror / legacy / omtk → DEFAULT_ADMIN (FK-safe actor).
            if (
              !req.currentUser ||
              (typeof User !== 'undefined' &&
                User.INTERNAL &&
                req.currentUser.id === User.INTERNAL.id)
            ) {
              const serviceUser = await resolveServiceUser();
              if (serviceUser) {
                req.currentUser = serviceUser;
              } else if (!req.currentUser && typeof User !== 'undefined' && User.INTERNAL) {
                req.currentUser = User.INTERNAL;
              }
            }

            return next();
          },
        },
      },
    },
  };
};
