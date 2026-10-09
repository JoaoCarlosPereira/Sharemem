/*!
 * Mem0 Shared — hooks mem0-auth e current-user sem Sails lift:
 *   - socket do embed (JWT) entra na sala `@user:<id>` (revogação funciona);
 *   - cookie JWT em `/attachments/*` preenche `req.mem0Auth` (escopo por grupo).
 * Run: node --test test/unit/mem0-auth-hooks.test.js
 */

const assert = require('assert');
const jwt = require('jsonwebtoken');
const { afterEach, beforeEach, describe, it } = require('node:test');

const defineMem0AuthHook = require('../../api/hooks/mem0-auth');
const defineCurrentUserHook = require('../../api/hooks/current-user');

const SECRET = 'unit-test-secret-value-32bytes!!';
const USER = { id: 'u-42', email: 'pessoa@empresa.com', role: 'admin', isDeactivated: false };

const sign = (claims) =>
  jwt.sign({ sub: USER.email, email: USER.email, mem0: true, ...claims }, SECRET, {
    algorithm: 'HS256',
  });

const makeSails = (joined) => ({
  log: { info: () => {}, warn: () => {} },
  sockets: { join: (req, room) => joined.push(room) },
  helpers: { mem0: { upsertUserByEmail: { with: async () => USER } } },
});

const makeRes = () => ({
  statusCode: 200,
  status(code) {
    this.statusCode = code;
    return this;
  },
  json(body) {
    this.body = body;
    return this;
  },
});

const runBefore = async (hook, route, req) => {
  const res = makeRes();
  let nextCalled = false;
  await hook.routes.before[route].fn(req, res, () => {
    nextCalled = true;
  });
  return { res, nextCalled };
};

describe('mem0-auth/current-user hooks (ponte ativa)', () => {
  const prevSecret = process.env.AUTH_JWT_SECRET;
  let joined;

  beforeEach(() => {
    process.env.AUTH_JWT_SECRET = SECRET;
    joined = [];
    global.User = {
      INTERNAL: { id: 'internal' },
      qm: { getOneByEmail: async (email) => (email === USER.email ? USER : null) },
    };
  });

  afterEach(() => {
    if (prevSecret === undefined) delete process.env.AUTH_JWT_SECRET;
    else process.env.AUTH_JWT_SECRET = prevSecret;
    delete global.User;
  });

  it('socket JWT do embed entra na sala @user:<id>', async () => {
    const hook = defineMem0AuthHook(makeSails(joined));
    const req = {
      isSocket: true,
      path: '/api/projects',
      headers: { authorization: `Bearer ${sign({ group: 'group-a' })}` },
    };

    const { nextCalled } = await runBefore(hook, '/api/*', req);

    assert.strictEqual(nextCalled, true);
    assert.strictEqual(req.currentUser, USER);
    assert.deepStrictEqual(joined, ['@user:u-42']);
  });

  it('HTTP JWT (não socket) não tenta entrar em sala', async () => {
    const hook = defineMem0AuthHook(makeSails(joined));
    const req = {
      isSocket: false,
      path: '/api/projects',
      headers: { authorization: `Bearer ${sign({ group: 'group-a' })}` },
    };

    await runBefore(hook, '/api/*', req);

    assert.deepStrictEqual(joined, []);
  });

  it('cookie JWT em /attachments/* preenche req.mem0Auth com o grupo', async () => {
    const hook = defineCurrentUserHook(makeSails(joined));
    const req = { headers: {}, cookies: { accessToken: sign({ group: 'group-a' }) } };

    const { nextCalled } = await runBefore(hook, '/attachments/*', req);

    assert.strictEqual(nextCalled, true);
    assert.strictEqual(req.currentUser, USER);
    assert.deepStrictEqual(
      { method: req.mem0Auth.method, group: req.mem0Auth.group },
      { method: 'jwt', group: 'group-a' },
    );
  });
});
