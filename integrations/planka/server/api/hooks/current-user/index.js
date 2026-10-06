/*!
 * Copyright (c) 2024 PLANKA Software GmbH
 * Licensed under the Fair Use License: https://github.com/plankanban/planka/blob/master/LICENSE.md
 */

/**
 * current-user hook
 *
 * @description :: A hook definition. Extends Sails by adding shadow routes, implicit actions,
 *                 and/or initialization logic.
 * @docs        :: https://sailsjs.com/docs/concepts/extending-sails/hooks
 *
 * Mem0 Shared: JWT de embed (`mem0: true`, AUTH_JWT_SECRET) é resolvido pelo
 * hook `mem0-auth` (upsert por e-mail). Este hook não deve sobrescrever.
 */

const jwt = require('jsonwebtoken');

module.exports = function defineCurrentUserHook(sails) {
  const TOKEN_PATTERN = /^Bearer /;
  const API_KEY_HEADER_NAME = 'x-api-key';

  const isMem0EmbedJwt = (accessToken) => {
    const secret = String(process.env.AUTH_JWT_SECRET || '').trim();
    if (!secret || !accessToken) return false;
    try {
      const payload = jwt.verify(accessToken, secret, { algorithms: ['HS256'] });
      return Boolean(payload && payload.mem0 === true);
    } catch (_err) {
      return false;
    }
  };

  const getSessionAndUserByAccessToken = async (accessToken, httpOnlyToken) => {
    let payload;
    try {
      payload = sails.helpers.utils.verifyJwtToken(accessToken);
    } catch (error) {
      return null;
    }

    const session = await Session.qm.getOneUndeletedByAccessToken(accessToken);

    if (!session) {
      return null;
    }

    if (session.httpOnlyToken && httpOnlyToken !== session.httpOnlyToken) {
      return null;
    }

    const user = await User.qm.getOneById(payload.subject, {
      withDeactivated: false,
    });

    if (!user) {
      return null;
    }

    if (user.passwordChangedAt > payload.issuedAt) {
      return null;
    }

    return {
      session,
      user,
    };
  };

  const getUserByApiKey = (apiKey) => {
    const apiKeyHash = sails.helpers.utils.hash(apiKey);

    return User.qm.getOneActiveByApiKeyHash(apiKeyHash);
  };

  return {
    /**
     * Runs when this Sails app loads/lifts.
     */

    async initialize() {
      sails.log.info('Initializing custom hook (`current-user`)');
    },

    routes: {
      before: {
        '/api/*': {
          async fn(req, res, next) {
            const { authorization: authorizationHeader, [API_KEY_HEADER_NAME]: apiKey } =
              req.headers;

            if (authorizationHeader && TOKEN_PATTERN.test(authorizationHeader)) {
              const accessToken = authorizationHeader.replace(TOKEN_PATTERN, '');
              const { internalAccessToken } = sails.config.custom;

              if (internalAccessToken && accessToken === internalAccessToken) {
                req.currentUser = User.INTERNAL;
              } else if (isMem0EmbedJwt(accessToken)) {
                // Deixa mem0-auth fazer upsert + req.currentUser (ADR-008).
              } else {
                const { httpOnlyToken } = req.cookies;

                const sessionAndUser = await getSessionAndUserByAccessToken(
                  accessToken,
                  httpOnlyToken,
                );

                if (sessionAndUser) {
                  const { session, user } = sessionAndUser;

                  if (user.language) {
                    req.setLocale(user.language);
                  }

                  Object.assign(req, {
                    currentSession: session,
                    currentUser: user,
                  });

                  if (req.isSocket) {
                    sails.sockets.join(req, `@accessToken:${session.accessToken}`);
                    sails.sockets.join(req, `@user:${user.id}`);
                  }
                }
              }
            } else if (apiKey) {
              const user = await getUserByApiKey(apiKey);

              if (user) {
                if (user.language) {
                  req.setLocale(user.language);
                }

                req.currentUser = user;

                if (req.isSocket) {
                  sails.sockets.join(req, `@user:${user.id}`);
                }
              }
            }

            return next();
          },
        },
        '/attachments/*': {
          async fn(req, res, next) {
            const { accessToken, httpOnlyToken } = req.cookies;

            if (accessToken) {
              if (isMem0EmbedJwt(accessToken)) {
                // mem0-auth cobre /api/*; attachments usam cookie — re-resolve aqui.
                try {
                  const secret = String(process.env.AUTH_JWT_SECRET || '').trim();
                  const payload = jwt.verify(accessToken, secret, { algorithms: ['HS256'] });
                  if (typeof User !== 'undefined' && User.qm) {
                    const email = String(payload.email || '')
                      .trim()
                      .toLowerCase();
                    if (email) {
                      const user = await User.qm.getOneByEmail(email);
                      if (user && !user.isDeactivated) {
                        req.currentUser = user;
                        // Mesmo formato do mem0-auth: o escopo por grupo
                        // (hasAdminAccessToSharedProject) depende dele.
                        req.mem0Auth = {
                          method: 'jwt',
                          subject: payload.sub ? String(payload.sub) : undefined,
                          email,
                          group: payload.group ? String(payload.group) : undefined,
                        };
                      }
                    }
                  }
                } catch (_err) {
                  // leave unset; policy will 401
                }
              } else {
                const sessionAndUser = await getSessionAndUserByAccessToken(
                  accessToken,
                  httpOnlyToken,
                );

                if (sessionAndUser) {
                  const { session, user } = sessionAndUser;

                  Object.assign(req, {
                    currentSession: session,
                    currentUser: user,
                  });
                }
              }
            } else {
              const { [API_KEY_HEADER_NAME]: apiKey } = req.headers;

              if (apiKey) {
                const user = await getUserByApiKey(apiKey);

                if (user) {
                  req.currentUser = user;
                }
              }
            }

            return next();
          },
        },
      },
    },
  };
};
