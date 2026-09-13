/*!
 * Unit tests for Mem0 PLANKA auth bridge (no Sails lift).
 * Run: npm test -- --grep mem0-auth  (from server/) or:
 *   node --test test/unit/mem0-auth.test.js
 */

const assert = require('assert');
const crypto = require('crypto');
const jwt = require('jsonwebtoken');
const { describe, it } = require('node:test');

const {
  bearerToken,
  authenticateMem0Request,
  authenticateOmtk,
} = require('../../api/hooks/mem0-auth/lib/validate-auth');
const defineMem0AuthHook = require('../../api/hooks/mem0-auth');

const SECRET = 'unit-test-secret-value-32bytes!!';

describe('mem0-auth validate-auth', () => {
  it('bearerToken parses Authorization header', () => {
    assert.strictEqual(bearerToken('Bearer abc'), 'abc');
    assert.strictEqual(bearerToken('bearer xyz'), 'xyz');
    assert.strictEqual(bearerToken(''), '');
    assert.strictEqual(bearerToken(undefined), '');
  });

  it('bridge disabled when AUTH_JWT_SECRET empty', () => {
    const r = authenticateMem0Request({
      authorizationHeader: '',
      env: { AUTH_JWT_SECRET: '' },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'disabled');
  });

  it('allows the bootstrap route without a token so the login screen can load', () => {
    const r = authenticateMem0Request({
      authorizationHeader: '',
      path: '/api/bootstrap',
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'public');
  });

  it('keeps protected API routes fail-closed without a token', () => {
    const r = authenticateMem0Request({
      authorizationHeader: '',
      path: '/api/users/me',
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.reason, 'missing_token');
  });

  it('accepts HS256 JWT with sub', () => {
    const token = jwt.sign({ sub: 'user-1', email: 'a@example.com' }, SECRET, {
      algorithm: 'HS256',
    });
    const r = authenticateMem0Request({
      authorizationHeader: `Bearer ${token}`,
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'jwt');
    assert.strictEqual(r.subject, 'user-1');
  });

  it('propagates name picture and mem0 claim from JWT', () => {
    const token = jwt.sign(
      {
        sub: 'joao@example.com',
        email: 'joao@example.com',
        name: 'João',
        picture: 'https://lh3.example/p.jpg',
        mem0: true,
      },
      SECRET,
      { algorithm: 'HS256' },
    );
    const r = authenticateMem0Request({
      authorizationHeader: `Bearer ${token}`,
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'jwt');
    assert.strictEqual(r.email, 'joao@example.com');
    assert.strictEqual(r.name, 'João');
    assert.strictEqual(r.picture, 'https://lh3.example/p.jpg');
    assert.strictEqual(r.mem0, true);
  });

  it('propagates the trusted group claim from JWT (kanban-board-group-isolation)', () => {
    const token = jwt.sign(
      {
        sub: 'u1@example.com',
        email: 'u1@example.com',
        group: '11111111-2222-3333-4444-555555555555',
      },
      SECRET,
      { algorithm: 'HS256' },
    );
    const r = authenticateMem0Request({
      authorizationHeader: `Bearer ${token}`,
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'jwt');
    assert.strictEqual(r.group, '11111111-2222-3333-4444-555555555555');
  });

  it('leaves group undefined when the JWT carries no group (fail-closed downstream)', () => {
    const token = jwt.sign({ sub: 'u2@example.com', email: 'u2@example.com' }, SECRET, {
      algorithm: 'HS256',
    });
    const r = authenticateMem0Request({
      authorizationHeader: `Bearer ${token}`,
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.group, undefined);
  });

  it('rejects JWT signed with wrong secret', () => {
    const token = jwt.sign({ sub: 'user-1' }, 'other-secret-other-secret-xxxx', {
      algorithm: 'HS256',
    });
    const r = authenticateMem0Request({
      authorizationHeader: `Bearer ${token}`,
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.reason, 'invalid_jwt');
  });

  it('rejects JWT without sub', () => {
    const token = jwt.sign({ email: 'a@example.com' }, SECRET, {
      algorithm: 'HS256',
    });
    const r = authenticateMem0Request({
      authorizationHeader: `Bearer ${token}`,
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.reason, 'missing_sub');
  });

  it('allows public routes with query strings', () => {
    const r = authenticateMem0Request({
      authorizationHeader: '',
      path: '/api/terms?lang=pt-BR',
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'public');
  });

  it('accepts Bearer local when MEM0_AUTH_ALLOW_LEGACY=1', () => {
    const r = authenticateMem0Request({
      authorizationHeader: 'Bearer local',
      env: { AUTH_JWT_SECRET: SECRET, MEM0_AUTH_ALLOW_LEGACY: '1' },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'legacy');
  });

  it('rejects Bearer local when legacy disabled', () => {
    const r = authenticateMem0Request({
      authorizationHeader: 'Bearer local',
      env: { AUTH_JWT_SECRET: SECRET, MEM0_AUTH_ALLOW_LEGACY: '0' },
    });
    assert.strictEqual(r.ok, false);
  });

  it('accepts INTERNAL_ACCESS_TOKEN', () => {
    const r = authenticateMem0Request({
      authorizationHeader: 'Bearer super-internal',
      env: { AUTH_JWT_SECRET: SECRET, INTERNAL_ACCESS_TOKEN: 'super-internal' },
    });
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'internal');
  });

  it('flags omtk_ for async lookup', () => {
    const r = authenticateMem0Request({
      authorizationHeader: 'Bearer omtk_abc',
      env: { AUTH_JWT_SECRET: SECRET },
    });
    assert.strictEqual(r.needsOmtkLookup, true);
    assert.strictEqual(r.token, 'omtk_abc');
  });

  it('authenticateOmtk accepts valid non-revoked token', async () => {
    const raw = 'omtk_testtoken';
    const digest = crypto.createHash('sha256').update(raw, 'utf8').digest('hex');
    const db = {
      async query(sql, params) {
        assert.ok(sql.includes('agent_tokens'));
        assert.strictEqual(params[0], digest);
        return { rows: [{ user_id: 'agent-9', revoked_at: null }] };
      },
    };
    const r = await authenticateOmtk(raw, db);
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.method, 'omtk_');
    assert.strictEqual(r.subject, 'agent-9');
  });

  it('authenticateOmtk rejects revoked token', async () => {
    const db = {
      async query() {
        return { rows: [{ user_id: 'agent-9', revoked_at: new Date() }] };
      },
    };
    const r = await authenticateOmtk('omtk_x', db);
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.reason, 'omtk_invalid');
  });
});

/**
 * Regression: `ensureSharedAccess` (mem0-auth/index.js) called
 * `ProjectManager.qm.destroyOne` / `BoardMembership.qm.destroyOne`, methods
 * that don't exist on the query-methods hooks (only `deleteOne` is exported).
 * Every JWT embed request threw a TypeError, was swallowed by the outer
 * try/catch (only `err.message` logged, so the failure was invisible), and
 * no BoardMembership row was ever created — the PLANKA board never finished
 * loading for the mem0 embed user (kanban "loading forever" bug).
 */
describe('mem0-auth ensureSharedAccess (hook, mocked Waterline globals)', () => {
  const makeSails = () => ({
    log: { info() {}, warn() {} },
    async sendNativeQuery() {
      return { rows: [] };
    },
    helpers: {
      mem0: {
        upsertUserByEmail: {
          async with({ email }) {
            return { id: 'user-1', email, language: null };
          },
        },
      },
    },
  });

  const withGlobals = (globals, fn) => {
    const previous = {};
    for (const key of Object.keys(globals)) {
      previous[key] = global[key];
      global[key] = globals[key];
    }
    const restore = () => {
      for (const key of Object.keys(globals)) {
        global[key] = previous[key];
      }
    };
    return Promise.resolve()
      .then(fn)
      .then(
        (value) => {
          restore();
          return value;
        },
        (err) => {
          restore();
          throw err;
        },
      );
  };

  const embedToken = (extra = {}) =>
    jwt.sign(
      { sub: 'ui-user', email: 'ui-user@mem0.local', mem0: true, ...extra },
      SECRET,
      { algorithm: 'HS256' },
    );

  const runAuthMiddleware = async (sails, token) => {
    const hook = defineMem0AuthHook(sails);
    const fn = hook.routes.before['/api/*'].fn;
    let nextCalled = false;
    const req = { headers: { authorization: `Bearer ${token}` }, path: '/api/projects' };
    const res = {
      status() {
        return this;
      },
      json() {
        return this;
      },
    };
    await fn(req, res, () => {
      nextCalled = true;
    });
    return { req, nextCalled };
  };

  it('grants shared "*" access via deleteOne/createOne without throwing', async () => {
    const priorEnv = process.env.AUTH_JWT_SECRET;
    process.env.AUTH_JWT_SECRET = SECRET;

    const deletedProjectManagerIds = [];
    const createdBoardMemberships = [];
    const warnings = [];

    const sails = makeSails();
    sails.log.warn = (...args) => warnings.push(args);

    try {
      await withGlobals(
        {
          User: { qm: {} },
          Project: { qm: { async getShared() { return [{ id: 'p1' }]; } } },
          ProjectManager: {
            qm: {
              async getByUserId() { return [{ id: 'pm-stale' }]; },
              async deleteOne(id) { deletedProjectManagerIds.push(id); },
            },
          },
          Board: { qm: { async getByProjectIds() { return [{ id: 'b1' }]; } } },
          BoardMembership: {
            Roles: { EDITOR: 'editor' },
            qm: {
              async getOneByBoardIdAndUserId() { return null; },
              async createOne(values) { createdBoardMemberships.push(values); },
            },
          },
        },
        () => runAuthMiddleware(sails, embedToken({ group: '*' })),
      );
    } finally {
      process.env.AUTH_JWT_SECRET = priorEnv;
    }

    assert.deepStrictEqual(warnings, [], 'ensureSharedAccess must not throw/warn');
    assert.deepStrictEqual(deletedProjectManagerIds, ['pm-stale']);
    assert.strictEqual(createdBoardMemberships.length, 1);
    assert.strictEqual(createdBoardMemberships[0].boardId, 'b1');
  });

  it('revokes a stale membership via deleteOne when the board moved to another group', async () => {
    const priorEnv = process.env.AUTH_JWT_SECRET;
    process.env.AUTH_JWT_SECRET = SECRET;

    const deletedBoardMembershipIds = [];
    const warnings = [];

    const sails = makeSails();
    sails.log.warn = (...args) => warnings.push(args);

    try {
      await withGlobals(
        {
          User: { qm: {} },
          Project: { qm: { async getShared() { return [{ id: 'p1' }]; } } },
          ProjectManager: {
            qm: {
              async getByUserId() { return []; },
              async deleteOne() {},
            },
          },
          Board: { qm: { async getByProjectIds() { return [{ id: 'b1' }]; } } },
          BoardMembership: {
            Roles: { EDITOR: 'editor' },
            qm: {
              async getOneByBoardIdAndUserId() {
                return { id: 'bm-old', role: 'editor' };
              },
              async deleteOne(id) { deletedBoardMembershipIds.push(id); },
            },
          },
        },
        // group "group-b" != the board's actual group ("group-a" from
        // get-board-group-ids, empty here since spec_planka_id_map isn't
        // mocked) — mismatched group hits the revoke branch.
        () => runAuthMiddleware(sails, embedToken({ group: 'group-b' })),
      );
    } finally {
      process.env.AUTH_JWT_SECRET = priorEnv;
    }

    assert.deepStrictEqual(warnings, [], 'ensureSharedAccess must not throw/warn');
    assert.deepStrictEqual(deletedBoardMembershipIds, ['bm-old']);
  });
});
