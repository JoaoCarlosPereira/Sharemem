/*!
 * Mem0 Shared — isolamento por grupo sem Sails lift: controllers
 * projects/index e projects/show, destinatários de socket (make-scoper) e
 * idempotência de ensureSharedAccess.
 * Run: node --test test/unit/mem0-group-scope.test.js
 */

const assert = require('assert');
const { afterEach, beforeEach, describe, it } = require('node:test');
const lodash = require('lodash');

const GROUP_A = 'group-a';
const GROUP_B = 'group-b';

// Mundo: grupo A tem p-a/b-a, grupo B tem p-b/b-b. Todos são ADMIN (ShareMem).
const world = {
  projects: [
    { id: 'p-a', name: 'Projeto A', ownerProjectManagerId: null },
    { id: 'p-b', name: 'Projeto B', ownerProjectManagerId: null },
  ],
  boards: [
    { id: 'b-a', projectId: 'p-a', creatorUserId: 'admin-internal' },
    { id: 'b-b', projectId: 'p-b', creatorUserId: 'admin-internal' },
  ],
  boardGroups: { 'b-a': GROUP_A, 'b-b': GROUP_B },
  boardMemberships: [],
  projectManagers: [],
};

const byIds = (records, ids, attr = 'id') =>
  records.filter((record) => ids.map(String).includes(String(record[attr])));

const installGlobals = () => {
  global._ = lodash;
  global.User = {
    Roles: { ADMIN: 'admin' },
    qm: { getByIds: async () => [] },
  };
  global.Project = {
    qm: {
      getOneById: async (id) => world.projects.find((p) => p.id === id) || null,
      getByIds: async (ids) => byIds(world.projects, ids),
      getShared: async ({ exceptIdOrIds = [] } = {}) =>
        world.projects.filter((p) => !p.ownerProjectManagerId && !exceptIdOrIds.includes(p.id)),
    },
  };
  global.Board = {
    qm: {
      getByIds: async (ids, { exceptProjectIdOrIds = [] } = {}) =>
        byIds(world.boards, ids).filter((b) => !exceptProjectIdOrIds.includes(b.projectId)),
      getByProjectIds: async (ids) => byIds(world.boards, ids, 'projectId'),
      getByProjectId: async (id) => world.boards.filter((b) => b.projectId === id),
    },
  };
  global.BoardMembership = {
    qm: {
      getByUserId: async (userId) => world.boardMemberships.filter((m) => m.userId === userId),
      getByProjectIdAndUserId: async (projectId, userId) =>
        world.boardMemberships.filter((m) => m.projectId === projectId && m.userId === userId),
      getByProjectId: async (projectId) =>
        world.boardMemberships.filter((m) => m.projectId === projectId),
    },
  };
  global.ProjectManager = {
    qm: {
      getByProjectIds: async (ids) => byIds(world.projectManagers, ids, 'projectId'),
      getByProjectId: async (id) => world.projectManagers.filter((m) => m.projectId === id),
    },
  };
  const empty = { qm: new Proxy({}, { get: () => async () => [] }) };
  global.ProjectFavorite = empty;
  global.BackgroundImage = empty;
  global.BaseCustomFieldGroup = empty;
  global.CustomField = empty;
  global.NotificationService = empty;

  global.sails = {
    log: { warn: () => {} },
    sendNativeQuery: async (sql, values) => {
      if (sql.includes("entity_type = 'board'")) {
        return {
          rows: values[0]
            .filter((id) => world.boardGroups[id])
            .map((id) => ({ board_id: id, group_id: world.boardGroups[id] })),
        };
      }
      if (sql.includes("entity_type = 'project'")) return { rows: [] };
      if (sql.includes('FROM planka.user_account')) return { rows: [] };
      throw new Error(`unexpected query: ${sql}`);
    },
    helpers: {
      utils: {
        mapRecords: (records, attribute = 'id', unique = false) => {
          const result = lodash.map(records, attribute);
          return unique ? lodash.uniq(result) : result;
        },
      },
      users: {
        getManagerProjectIds: async (userId) =>
          world.projectManagers.filter((m) => m.userId === userId).map((m) => m.projectId),
        isProjectManager: async (userId, projectId) =>
          world.projectManagers.some((m) => m.userId === userId && m.projectId === projectId),
        isProjectFavorite: async () => false,
        presentMany: (users) => users,
        getAllActiveIds: async () => ['u-a', 'u-b'],
      },
      projects: {
        getManagerUserIds: async (projectId) =>
          world.projectManagers.filter((m) => m.projectId === projectId).map((m) => m.userId),
      },
      backgroundImages: { presentMany: (images) => images },
    },
  };
};

const userA = { id: 'u-a', email: 'a@empresa.com', role: 'admin' };

const jwtReq = (group) => ({
  currentUser: userA,
  mem0Auth: { method: 'jwt', group },
});

const call = (controller, req, inputs = {}) => controller.fn.call({ req }, inputs);

const loadController = (name) => {
  const path = require.resolve(`../../api/controllers/projects/${name}`);
  delete require.cache[path];
  return require(path); // eslint-disable-line global-require, import/no-dynamic-require
};

describe('mem0 group scope — controllers projects/index e projects/show', () => {
  beforeEach(() => {
    installGlobals();
    world.boardMemberships = [
      { id: 'bm-a', boardId: 'b-a', projectId: 'p-a', userId: 'u-a', role: 'editor' },
    ];
    world.projectManagers = [];
  });

  it('index: ADMIN do grupo A não recebe projeto do grupo B (sem atalho ADMIN)', async () => {
    const result = await call(loadController('index'), jwtReq(GROUP_A));

    assert.deepStrictEqual(
      result.items.map(({ id }) => id),
      ['p-a'],
    );
    assert.deepStrictEqual(
      result.included.boards.map(({ id }) => id),
      ['b-a'],
    );
  });

  it('index: falha ao resolver grupo = resposta vazia (fail-closed)', async () => {
    sails.sendNativeQuery = async () => {
      throw new Error('db down');
    };

    const result = await call(loadController('index'), jwtReq(GROUP_A));

    assert.deepStrictEqual(result.items, []);
    assert.deepStrictEqual(result.included.boards, []);
  });

  it('index: modo legado (group "*") mantém visão total do ADMIN', async () => {
    const result = await call(loadController('index'), jwtReq('*'));

    assert.deepStrictEqual(result.items.map(({ id }) => id).sort(), ['p-a', 'p-b']);
  });

  it('show: projeto de outro grupo → 404 para ADMIN sem membership', async () => {
    await assert.rejects(call(loadController('show'), jwtReq(GROUP_A), { id: 'p-b' }), {
      projectNotFound: 'Project not found',
    });
  });

  it('show: membership antiga em board de outro grupo ainda é 404', async () => {
    world.boardMemberships.push({
      id: 'bm-stale',
      boardId: 'b-b',
      projectId: 'p-b',
      userId: 'u-a',
      role: 'editor',
    });

    await assert.rejects(call(loadController('show'), jwtReq(GROUP_A), { id: 'p-b' }), {
      projectNotFound: 'Project not found',
    });
  });

  it('show: projeto do próprio grupo → 200 só com boards do grupo', async () => {
    const result = await call(loadController('show'), jwtReq(GROUP_A), { id: 'p-a' });

    assert.strictEqual(result.item.id, 'p-a');
    assert.deepStrictEqual(
      result.included.boards.map(({ id }) => id),
      ['b-a'],
    );
  });

  it('show: modo legado (group "*") abre projeto de qualquer grupo', async () => {
    const result = await call(loadController('show'), jwtReq('*'), { id: 'p-b' });

    assert.strictEqual(result.item.id, 'p-b');
  });
});

describe('mem0 group scope — destinatários de socket (make-scoper)', () => {
  const prevSecret = process.env.AUTH_JWT_SECRET;
  let makeScoper;

  beforeEach(() => {
    installGlobals();
    world.projectManagers = [{ id: 'pm-1', projectId: 'p-b', userId: 'u-b' }];
    world.boardMemberships = [
      { id: 'bm-b', boardId: 'b-b', projectId: 'p-b', userId: 'u-b2', role: 'editor' },
    ];
    makeScoper = require('../../api/helpers/projects/make-scoper'); // eslint-disable-line global-require
  });

  afterEach(() => {
    if (prevSecret === undefined) delete process.env.AUTH_JWT_SECRET;
    else process.env.AUTH_JWT_SECRET = prevSecret;
  });

  const scoperFor = (projectId) =>
    makeScoper.fn({ record: world.projects.find(({ id }) => id === projectId) });

  it('ponte mem0 ativa: ADMIN de outro grupo não recebe evento do projeto', async () => {
    process.env.AUTH_JWT_SECRET = 'secret';
    const scoper = scoperFor('p-b');

    assert.deepStrictEqual(await scoper.getUserIdsWithFullProjectVisibility(), ['u-b']);
    assert.deepStrictEqual((await scoper.getProjectRelatedUserIds()).sort(), ['u-b', 'u-b2']);
  });

  it('PLANKA upstream (sem ponte): ADMIN segue com visão total', async () => {
    delete process.env.AUTH_JWT_SECRET;
    const scoper = scoperFor('p-b');

    assert.deepStrictEqual((await scoper.getUserIdsWithFullProjectVisibility()).sort(), [
      'u-a',
      'u-b',
    ]);
  });
});
