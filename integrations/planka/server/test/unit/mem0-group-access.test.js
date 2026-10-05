/*!
 * Mem0 Shared — ADMIN do grupo A, com a ponte ativa, não acessa recurso do
 * grupo B por ID (REST e download via cookie), não arquiva/conclui projeto sem
 * ser gerente e é expulso das salas de socket ao perder vínculo.
 * Sem Sails lift. Run: node --test test/unit/mem0-group-access.test.js
 */

const assert = require('assert');
const { afterEach, beforeEach, describe, it } = require('node:test');
const lodash = require('lodash');

const GROUP_A = 'group-a';
const GROUP_B = 'group-b';
const USER_A = { id: 'u-a', email: 'a@empresa.com', role: 'admin' };

// Grupo A: p-a/b-a; grupo B: p-b/b-b. Toda entidade abaixo de b-b é do grupo B.
const PROJECTS = {
  'p-a': { id: 'p-a', name: 'Projeto A', ownerProjectManagerId: null },
  'p-b': { id: 'p-b', name: 'Projeto B', ownerProjectManagerId: null },
};
const BOARDS = {
  'b-a': { id: 'b-a', projectId: 'p-a', creatorUserId: 'admin-internal' },
  'b-b': { id: 'b-b', projectId: 'p-b', creatorUserId: 'admin-internal' },
};
const BOARD_GROUPS = { 'b-a': GROUP_A, 'b-b': GROUP_B };

const pathOf = (boardId) => ({
  board: BOARDS[boardId],
  project: PROJECTS[BOARDS[boardId].projectId],
});

const PATHS = {
  board: { 'b-b': () => pathOf('b-b') },
  list: {
    'l-b': () => ({ ...pathOf('b-b'), list: { id: 'l-b', boardId: 'b-b', type: 'active' } }),
  },
  card: { 'c-b': () => ({ ...pathOf('b-b'), card: { id: 'c-b', boardId: 'b-b' } }) },
  taskList: { 'tl-b': () => ({ ...pathOf('b-b'), taskList: { id: 'tl-b' } }) },
  customFieldGroup: {
    'cfg-b': () => ({ ...pathOf('b-b'), customFieldGroup: { id: 'cfg-b' } }),
  },
  attachment: {
    'att-b': () => ({ ...pathOf('b-b'), attachment: { id: 'att-b', type: 'file', data: {} } }),
  },
};

let state;

// Imita o Sails helper: `.intercept(name, fn)` devolve uma promise.
const pathHelper = (kind) => ({
  getPathToProjectById: (id) => ({
    intercept: async (_name, toError) => {
      const resolve = PATHS[kind][id];
      if (!resolve) throw toError();
      return resolve();
    },
  }),
});

const chain = (value) => {
  const promise = Promise.resolve(value);
  promise.intercept = () => promise;
  return promise;
};

const installGlobals = () => {
  global._ = lodash;
  global.User = { Roles: { ADMIN: 'admin' }, qm: { getByIds: async () => [] } };
  global.Attachment = { Types: { FILE: 'file' } };
  global.Project = {
    BackgroundTypes: {},
    BACKGROUND_GRADIENTS: [],
    qm: { getOneById: async (id) => PROJECTS[id] || null },
  };
  global.Board = {
    qm: { getByIds: async (ids) => ids.map((id) => BOARDS[id]).filter(Boolean) },
  };
  global.ProjectManager = {
    qm: {
      getOneByProjectIdAndUserId: async (projectId, userId) =>
        state.projectManagers.find((m) => m.projectId === projectId && m.userId === userId) || null,
      getByProjectId: async () => [],
      deleteOne: async (id) => state.projectManagers.find((m) => m.id === id) || null,
    },
  };
  global.BoardMembership = {
    qm: {
      getOneByBoardIdAndUserId: async (boardId, userId) =>
        state.boardMemberships.find((m) => m.boardId === boardId && m.userId === userId) || null,
      getByProjectIdAndUserId: async (projectId, userId) =>
        state.boardMemberships.filter((m) => m.projectId === projectId && m.userId === userId),
      deleteOne: async (id) => state.boardMemberships.find((m) => m.id === id) || null,
    },
  };
  const noop = { qm: new Proxy({}, { get: () => async () => [] }) };
  ['BoardSubscription', 'Card', 'CardSubscription', 'CardMembership', 'TaskList', 'Task'].forEach(
    (name) => {
      global[name] = noop;
    },
  );
  global.Webhook = { Events: {}, qm: { getAll: async () => [] } };

  global.sails = {
    log: { warn: () => {} },
    sendNativeQuery: async (sql, values) => {
      if (sql.includes("entity_type = 'board'")) {
        return {
          rows: values[0]
            .filter((id) => BOARD_GROUPS[id])
            .map((id) => ({ board_id: id, group_id: BOARD_GROUPS[id] })),
        };
      }
      if (sql.includes('FROM public.users')) return { rows: [] };
      if (sql.includes('FROM planka.user_account')) return { rows: [] };
      throw new Error(`unexpected query: ${sql}`);
    },
    sockets: {
      removeRoomMembersFromRooms: (...args) => state.removedRooms.push(args.slice(0, 2)),
      addRoomMembersToRooms: () => {},
      broadcast: () => {},
    },
    helpers: {
      boards: { ...pathHelper('board'), getCardIds: async () => [] },
      lists: { ...pathHelper('list'), isFinite: () => true },
      cards: pathHelper('card'),
      taskLists: pathHelper('taskList'),
      customFieldGroups: pathHelper('customFieldGroup'),
      attachments: pathHelper('attachment'),
      users: {
        isProjectManager: async (userId, projectId) =>
          state.projectManagers.some((m) => m.userId === userId && m.projectId === projectId),
        isBoardSubscriber: async () => false,
        presentOne: (user) => user,
      },
      projects: {
        updateOne: { with: ({ record, values }) => chain({ ...record, ...values }) },
        getProjectManagersTotalById: async () => 1,
        getBoardIdsById: async (projectId) =>
          Object.values(BOARDS)
            .filter((b) => b.projectId === projectId)
            .map((b) => b.id),
        makeScoper: {
          with: () => ({
            getProjectManagerUserIds: async () => [],
            getBoardMembershipsForWholeProject: async () => [],
            getProjectRelatedUserIds: async () => [],
          }),
        },
      },
      mem0: { notifySpecProjectLifecycle: { with: async () => {} } },
      utils: {
        mapRecords: (records, attribute = 'id') => lodash.map(records, attribute),
        sendWebhooks: { with: () => {} },
      },
    },
  };
};

const load = (relativePath) => {
  const path = require.resolve(`../../api/${relativePath}`);
  delete require.cache[path];
  return require(path); // eslint-disable-line global-require, import/no-dynamic-require
};

const call = (controller, req, inputs) =>
  controller.fn.call({ req: { get: () => undefined, ...req } }, inputs, {});

const jwtReq = (group) => ({ currentUser: USER_A, mem0Auth: { method: 'jwt', group } });
// Cookie de /attachments resolvido sem mem0Auth (cenário reproduzido na revisão).
const cookieReq = () => ({ currentUser: USER_A });

const prevSecret = process.env.AUTH_JWT_SECRET;
const restoreSecret = () => {
  if (prevSecret === undefined) delete process.env.AUTH_JWT_SECRET;
  else process.env.AUTH_JWT_SECRET = prevSecret;
};

const setup = () => {
  installGlobals();
  process.env.AUTH_JWT_SECRET = 'secret';
  state = {
    boardMemberships: [
      { id: 'bm-a', boardId: 'b-a', projectId: 'p-a', userId: 'u-a', role: 'editor' },
    ],
    projectManagers: [],
    removedRooms: [],
  };
};

describe('mem0 group access — ADMIN do grupo A com IDs do grupo B (ponte ativa)', () => {
  beforeEach(setup);
  afterEach(restoreSecret);

  const CASES = [
    ['boards/show', 'controllers/boards/show', { id: 'b-b' }, { boardNotFound: 'Board not found' }],
    ['cards/show', 'controllers/cards/show', { id: 'c-b' }, { cardNotFound: 'Card not found' }],
    [
      'cards/index',
      'controllers/cards/index',
      { listId: 'l-b' },
      { listNotFound: 'List not found' },
    ],
    ['lists/show', 'controllers/lists/show', { id: 'l-b' }, { listNotFound: 'List not found' }],
    [
      'comments/index',
      'controllers/comments/index',
      { cardId: 'c-b' },
      { cardNotFound: 'Card not found' },
    ],
    [
      'actions (board)',
      'controllers/actions/index-in-board',
      { boardId: 'b-b' },
      { boardNotFound: 'Board not found' },
    ],
    [
      'actions (card)',
      'controllers/actions/index-in-card',
      { cardId: 'c-b' },
      { cardNotFound: 'Card not found' },
    ],
    [
      'task-lists/show',
      'controllers/task-lists/show',
      { id: 'tl-b' },
      { taskListNotFound: 'Task list not found' },
    ],
    [
      'custom-field-groups/show',
      'controllers/custom-field-groups/show',
      { id: 'cfg-b' },
      { customFieldGroupNotFound: 'Custom field group not found' },
    ],
    [
      'projects/update (isFavorite)',
      'controllers/projects/update',
      { id: 'p-b', isFavorite: true },
      { projectNotFound: 'Project not found' },
    ],
    [
      'projects/update (isArchived)',
      'controllers/projects/update',
      { id: 'p-b', isArchived: true },
      { projectNotFound: 'Project not found' },
    ],
    [
      'projects/update (isCompleted)',
      'controllers/projects/update',
      { id: 'p-b', isCompleted: true },
      { projectNotFound: 'Project not found' },
    ],
    [
      'project-managers/create',
      'controllers/project-managers/create',
      { projectId: 'p-b', userId: 'u-a' },
      { projectNotFound: 'Project not found' },
    ],
    [
      'file-attachments/download',
      'controllers/file-attachments/download',
      { id: 'att-b' },
      { fileAttachmentNotFound: 'File attachment not found' },
    ],
  ];

  CASES.forEach(([label, controllerPath, inputs, expected]) => {
    it(`${label}: JWT do grupo A → 404`, async () => {
      await assert.rejects(call(load(controllerPath), jwtReq(GROUP_A), inputs), expected);
    });

    it(`${label}: sem mem0Auth (cookie / bearer de serviço) → 404`, async () => {
      await assert.rejects(call(load(controllerPath), cookieReq(), inputs), expected);
    });
  });

  it('projects/update: membership antiga em board do grupo B não libera (404)', async () => {
    state.boardMemberships.push({
      id: 'bm-stale',
      boardId: 'b-b',
      projectId: 'p-b',
      userId: 'u-a',
      role: 'editor',
    });

    await assert.rejects(
      call(load('controllers/projects/update'), jwtReq(GROUP_A), { id: 'p-b', isArchived: true }),
      { projectNotFound: 'Project not found' },
    );
  });
});

describe('mem0 group access — projects/update no próprio grupo', () => {
  beforeEach(setup);
  afterEach(restoreSecret);

  it('membro (não gerente) favorita, mas não arquiva nem conclui', async () => {
    const update = load('controllers/projects/update');

    const result = await call(update, jwtReq(GROUP_A), { id: 'p-a', isFavorite: true });
    assert.strictEqual(result.item.isFavorite, true);

    await assert.rejects(call(update, jwtReq(GROUP_A), { id: 'p-a', isArchived: true }), {
      notEnoughRights: 'Not enough rights',
    });
    await assert.rejects(call(update, jwtReq(GROUP_A), { id: 'p-a', isCompleted: true }), {
      notEnoughRights: 'Not enough rights',
    });
  });

  it('gerente arquiva e conclui', async () => {
    state.projectManagers.push({ id: 'pm-a', projectId: 'p-a', userId: 'u-a' });

    const result = await call(load('controllers/projects/update'), jwtReq(GROUP_A), {
      id: 'p-a',
      isArchived: true,
      isCompleted: true,
    });

    assert.strictEqual(result.item.isArchived, true);
    assert.strictEqual(result.item.isCompleted, true);
  });
});

describe('mem0 group access — hasAdminAccessToSharedProject (fail-closed)', () => {
  beforeEach(installGlobals);
  afterEach(restoreSecret);

  const { hasAdminAccessToSharedProject } = require('../../utils/mem0-group-scope'); // eslint-disable-line global-require
  const shared = PROJECTS['p-a'];

  it('ponte ativa: só o modo legado (group "*") tem atalho ADMIN', () => {
    process.env.AUTH_JWT_SECRET = 'secret';

    assert.strictEqual(hasAdminAccessToSharedProject(jwtReq(GROUP_A), shared), false);
    assert.strictEqual(hasAdminAccessToSharedProject(cookieReq(), shared), false);
    assert.strictEqual(
      hasAdminAccessToSharedProject(
        { currentUser: USER_A, mem0Auth: { method: 'internal' } },
        shared,
      ),
      false,
    );
    assert.strictEqual(hasAdminAccessToSharedProject(jwtReq('*'), shared), true);
    assert.strictEqual(
      hasAdminAccessToSharedProject(jwtReq('*'), { ...shared, ownerProjectManagerId: 'pm' }),
      false,
    );
  });

  it('PLANKA upstream (sem ponte): ADMIN mantém o atalho', () => {
    delete process.env.AUTH_JWT_SECRET;

    assert.strictEqual(hasAdminAccessToSharedProject(cookieReq(), shared), true);
  });
});

describe('mem0 group access — expulsão das salas de socket (ponte ativa)', () => {
  beforeEach(setup);
  afterEach(restoreSecret);

  it('board-memberships/delete-one: ADMIN sai de board:<id>', async () => {
    const membership = { id: 'bm-x', boardId: 'b-b', projectId: 'p-b', userId: 'u-a' };
    state.boardMemberships.push(membership);

    await load('helpers/board-memberships/delete-one').fn({
      record: membership,
      user: USER_A,
      project: PROJECTS['p-b'],
      board: BOARDS['b-b'],
      actorUser: USER_A,
    });

    assert.deepStrictEqual(state.removedRooms, [['@user:u-a', 'board:b-b']]);
  });

  it('project-managers/delete-one: ADMIN sai das salas dos boards do projeto', async () => {
    const manager = { id: 'pm-x', projectId: 'p-b', userId: 'u-a' };
    state.projectManagers.push(manager);

    await load('helpers/project-managers/delete-one').fn({
      record: manager,
      user: USER_A,
      project: PROJECTS['p-b'],
      actorUser: USER_A,
    });

    assert.deepStrictEqual(state.removedRooms, [['@user:u-a', 'board:b-b']]);
  });

  it('PLANKA upstream (sem ponte): ADMIN segue nas salas', async () => {
    delete process.env.AUTH_JWT_SECRET;
    const membership = { id: 'bm-x', boardId: 'b-b', projectId: 'p-b', userId: 'u-a' };
    state.boardMemberships.push(membership);

    await load('helpers/board-memberships/delete-one').fn({
      record: membership,
      user: USER_A,
      project: PROJECTS['p-b'],
      board: BOARDS['b-b'],
      actorUser: USER_A,
    });

    assert.deepStrictEqual(state.removedRooms, []);
  });
});
