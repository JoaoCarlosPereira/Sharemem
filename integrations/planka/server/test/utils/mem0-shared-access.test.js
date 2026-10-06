const assert = require('assert');

const {
  ensureSharedAccess,
  isInternalAdminUser,
  LEGACY_SHARED_GROUP,
} = require('../../utils/mem0-shared-access');

const GROUP_A = 'group-a';
const GROUP_B = 'group-b';

/**
 * Mock fiel ao contrato de api/hooks/query-methods/models/*.js: só expõe os
 * métodos que existem lá (deleteOne, NÃO destroyOne). Chamar um método
 * inexistente gera TypeError, exatamente como em produção.
 */
const makeWorld = ({
  projects,
  boards,
  boardMemberships = [],
  projectManagers = [],
  boardGroups = {},
  mappedProjectIds = [],
  sameGroupUserIds = [],
  groupedUserIds = [],
  cards = [],
  taskLists = [],
  tasks = [],
  boardSubscriptions = [],
  cardSubscriptions = [],
  cardMemberships = [],
  failures = {},
}) => {
  const state = {
    boardMemberships: boardMemberships.map((bm) => ({ ...bm })),
    projectManagers: projectManagers.map((pm) => ({ ...pm })),
    tasks: tasks.map((t) => ({ ...t })),
    boardSubscriptions: boardSubscriptions.map((s) => ({ ...s })),
    cardSubscriptions: cardSubscriptions.map((s) => ({ ...s })),
    cardMemberships: cardMemberships.map((m) => ({ ...m })),
    nextId: 1000,
  };
  // Critério Waterline simplificado: valor escalar = igualdade; array = IN.
  const matches = (record, criteria) =>
    Object.entries(criteria).every(([key, value]) =>
      Array.isArray(value) ? value.includes(record[key]) : record[key] === value,
    );
  const deleteWhere = (key) => async (criteria) => {
    const removed = state[key].filter((r) => matches(r, criteria));
    state[key] = state[key].filter((r) => !matches(r, criteria));
    return removed;
  };
  const maybeFail = (key) => {
    if (failures[key]) {
      throw new Error(`boom:${key}`);
    }
  };

  const models = {
    Project: {
      qm: {
        getShared: async () => projects.filter((p) => !p.ownerProjectManagerId),
      },
    },
    Board: {
      qm: {
        getByProjectIds: async ([projectId]) => {
          maybeFail(`boards:${projectId}`);
          return boards.filter((b) => b.projectId === projectId);
        },
      },
    },
    BoardMembership: {
      Roles: { EDITOR: 'editor', VIEWER: 'viewer' },
      qm: Object.freeze({
        getOneByBoardIdAndUserId: async (boardId, userId) => {
          maybeFail(`bm-get:${boardId}`);
          return (
            state.boardMemberships.find((bm) => bm.boardId === boardId && bm.userId === userId) ||
            null
          );
        },
        createOne: async (values) => {
          maybeFail(`bm-create:${values.boardId}`);
          state.nextId += 1;
          const record = { id: `bm-${state.nextId}`, ...values };
          state.boardMemberships.push(record);
          return record;
        },
        updateOne: async (id, values) => {
          const record = state.boardMemberships.find((bm) => bm.id === id);
          Object.assign(record, values);
          return record;
        },
        deleteOne: async (id) => {
          maybeFail(`bm-delete:${id}`);
          const index = state.boardMemberships.findIndex((bm) => bm.id === id);
          return index >= 0 ? state.boardMemberships.splice(index, 1)[0] : null;
        },
      }),
    },
    ProjectManager: {
      qm: Object.freeze({
        getByUserId: async (userId) => state.projectManagers.filter((pm) => pm.userId === userId),
        deleteOne: async (id) => {
          maybeFail(`pm-delete:${id}`);
          const index = state.projectManagers.findIndex((pm) => pm.id === id);
          return index >= 0 ? state.projectManagers.splice(index, 1)[0] : null;
        },
      }),
    },
    BoardSubscription: { qm: Object.freeze({ delete: deleteWhere('boardSubscriptions') }) },
    CardSubscription: { qm: Object.freeze({ delete: deleteWhere('cardSubscriptions') }) },
    CardMembership: { qm: Object.freeze({ delete: deleteWhere('cardMemberships') }) },
    Card: {
      qm: Object.freeze({
        getByBoardId: async (boardId) => cards.filter((c) => c.boardId === boardId),
      }),
    },
    TaskList: {
      qm: Object.freeze({
        getByCardIds: async (cardIds) => taskLists.filter((tl) => cardIds.includes(tl.cardId)),
      }),
    },
    Task: {
      qm: Object.freeze({
        update: async (criteria, values) => {
          const updated = state.tasks.filter((t) => matches(t, criteria));
          updated.forEach((t) => Object.assign(t, values));
          return updated;
        },
      }),
    },
  };

  const runQuery = async (sql, values) => {
    if (sql.includes("entity_type = 'board'")) {
      return {
        rows: values[0]
          .filter((id) => boardGroups[id])
          .map((id) => ({ board_id: id, group_id: boardGroups[id] })),
      };
    }
    if (sql.includes("entity_type = 'project'")) {
      maybeFail('project-map');
      return {
        rows: values[0]
          .filter((id) => mappedProjectIds.includes(id))
          .map((id) => ({ project_id: id })),
      };
    }
    if (sql.includes('FROM planka.user_account')) {
      return {
        rows: groupedUserIds.map((id) => ({ id, same_group: sameGroupUserIds.includes(id) })),
      };
    }
    throw new Error(`unexpected query: ${sql}`);
  };

  return { models, runQuery, state };
};

const user = { id: 'u-a', email: 'pessoa@empresa.com' };

describe('mem0-shared-access ensureSharedAccess', () => {
  const baseProjects = [
    { id: 'p-a', ownerProjectManagerId: null },
    { id: 'p-b', ownerProjectManagerId: null },
  ];
  const baseBoards = [
    { id: 'b-a1', projectId: 'p-a', creatorUserId: 'admin-internal' },
    { id: 'b-a2', projectId: 'p-a', creatorUserId: 'admin-internal' },
    { id: 'b-b1', projectId: 'p-b', creatorUserId: 'admin-internal' },
  ];
  const boardGroups = { 'b-a1': GROUP_A, 'b-a2': GROUP_A, 'b-b1': GROUP_B };

  it('cria membership EDITOR em todo board do grupo e revoga as de outro grupo', async () => {
    const world = makeWorld({
      projects: baseProjects,
      boards: baseBoards,
      boardGroups,
      boardMemberships: [
        { id: 'bm-old-b', boardId: 'b-b1', projectId: 'p-b', userId: 'u-a', role: 'editor' },
        { id: 'bm-viewer', boardId: 'b-a2', projectId: 'p-a', userId: 'u-a', role: 'viewer' },
      ],
    });

    const result = await ensureSharedAccess({
      user,
      userGroupId: GROUP_A,
      models: world.models,
      runQuery: world.runQuery,
    });

    assert.deepStrictEqual(result.errors, []);
    assert.strictEqual(result.created, 1);
    assert.strictEqual(result.promoted, 1);
    assert.strictEqual(result.revoked, 1);
    assert.deepStrictEqual(
      world.state.boardMemberships.map(({ boardId, role }) => `${boardId}:${role}`).sort(),
      ['b-a1:editor', 'b-a2:editor'],
    );
  });

  it('remove gerência só de projeto com board de outro grupo (qm.deleteOne, não destroyOne)', async () => {
    const world = makeWorld({
      projects: [...baseProjects, { id: 'p-mix', ownerProjectManagerId: null }],
      boards: [
        ...baseBoards,
        { id: 'b-mix-a', projectId: 'p-mix', creatorUserId: 'admin-internal' },
        { id: 'b-mix-b', projectId: 'p-mix', creatorUserId: 'admin-internal' },
      ],
      boardGroups: { ...boardGroups, 'b-mix-a': GROUP_A, 'b-mix-b': GROUP_B },
      mappedProjectIds: ['p-a', 'p-b', 'p-mix'],
      projectManagers: [
        { id: 'pm-a', projectId: 'p-a', userId: 'u-a' },
        { id: 'pm-b', projectId: 'p-b', userId: 'u-a' },
        { id: 'pm-mix', projectId: 'p-mix', userId: 'u-a' },
      ],
    });

    const result = await ensureSharedAccess({
      user,
      userGroupId: GROUP_A,
      models: world.models,
      runQuery: world.runQuery,
    });

    assert.deepStrictEqual(result.errors, []);
    assert.strictEqual(result.removedProjectManagers, 2);
    // I4: projeto só com boards do grupo (mesmo espelhado) mantém a gerência,
    // preservando a criação de boards pela UI.
    assert.deepStrictEqual(
      world.state.projectManagers.map(({ id }) => id),
      ['pm-a'],
    );
  });

  it('tira o usuário das salas board:<id> ao revogar membership/gerência', async () => {
    const world = makeWorld({
      projects: baseProjects,
      boards: baseBoards,
      boardGroups,
      boardMemberships: [
        { id: 'bm-old-b', boardId: 'b-b1', projectId: 'p-b', userId: 'u-a', role: 'editor' },
      ],
      projectManagers: [{ id: 'pm-b', projectId: 'p-b', userId: 'u-a' }],
    });
    const removed = [];
    const sockets = {
      removeRoomMembersFromRooms: (source, target) => removed.push(`${source}>${target}`),
    };

    await ensureSharedAccess({
      user,
      userGroupId: GROUP_A,
      models: world.models,
      runQuery: world.runQuery,
      sockets,
    });

    assert.deepStrictEqual(removed, ['@user:u-a>board:b-b1', '@user:u-a>board:b-b1']);
  });

  describe('limpeza de inscrições na revogação (vazamento por notificações)', () => {
    // Mesma regra de helpers/comments/create-one: notifica inscritos no card e no
    // board, exceto o autor, sem checar membership.
    const notifiedOnComment = (world, { boardId, cardId, authorId }) =>
      [
        ...new Set([
          ...world.state.cardSubscriptions.filter((s) => s.cardId === cardId).map((s) => s.userId),
          ...world.state.boardSubscriptions
            .filter((s) => s.boardId === boardId)
            .map((s) => s.userId),
        ]),
      ].filter((userId) => userId !== authorId);

    const leakWorld = (extra) =>
      makeWorld({
        projects: baseProjects,
        boards: baseBoards,
        boardGroups,
        cards: [
          { id: 'c-b1', boardId: 'b-b1' },
          { id: 'c-a1', boardId: 'b-a1' },
        ],
        taskLists: [
          { id: 'tl-b1', cardId: 'c-b1' },
          { id: 'tl-a1', cardId: 'c-a1' },
        ],
        tasks: [
          { id: 't-b1', taskListId: 'tl-b1', assigneeUserId: 'u-a' },
          { id: 't-a1', taskListId: 'tl-a1', assigneeUserId: 'u-a' },
        ],
        boardSubscriptions: [
          { id: 'bs-b1', boardId: 'b-b1', userId: 'u-a' },
          { id: 'bs-a1', boardId: 'b-a1', userId: 'u-a' },
        ],
        cardSubscriptions: [
          { id: 'cs-b1', cardId: 'c-b1', userId: 'u-a' },
          { id: 'cs-b1-y', cardId: 'c-b1', userId: 'u-y' },
          { id: 'cs-a1', cardId: 'c-a1', userId: 'u-a' },
        ],
        cardMemberships: [
          { id: 'cm-b1', cardId: 'c-b1', userId: 'u-a' },
          { id: 'cm-a1', cardId: 'c-a1', userId: 'u-a' },
        ],
        ...extra,
      });

    const assertOnlyGroupAKept = (world) => {
      assert.deepStrictEqual(
        world.state.boardSubscriptions.map(({ id }) => id),
        ['bs-a1'],
      );
      assert.deepStrictEqual(
        world.state.cardSubscriptions.map(({ id }) => id),
        ['cs-b1-y', 'cs-a1'],
      );
      assert.deepStrictEqual(
        world.state.cardMemberships.map(({ id }) => id),
        ['cm-a1'],
      );
      assert.deepStrictEqual(
        world.state.tasks.map(({ id, assigneeUserId }) => `${id}:${assigneeUserId}`),
        ['t-b1:null', 't-a1:u-a'],
      );
    };

    it('revogar membership de board do grupo Y remove inscrições e o comentário novo não o notifica', async () => {
      const world = leakWorld({
        boardMemberships: [
          { id: 'bm-old-b', boardId: 'b-b1', projectId: 'p-b', userId: 'u-a', role: 'editor' },
        ],
      });
      const comment = { boardId: 'b-b1', cardId: 'c-b1', authorId: 'u-y2' };
      assert.ok(notifiedOnComment(world, comment).includes('u-a'));

      const result = await ensureSharedAccess({
        user,
        userGroupId: GROUP_A,
        models: world.models,
        runQuery: world.runQuery,
      });

      assert.deepStrictEqual(result.errors, []);
      assert.strictEqual(result.revoked, 1);
      assertOnlyGroupAKept(world);
      assert.deepStrictEqual(notifiedOnComment(world, comment), ['u-y']);
    });

    it('revogar gerência de projeto com board do grupo Y remove inscrições mesmo sem membership', async () => {
      const world = leakWorld({
        projectManagers: [{ id: 'pm-b', projectId: 'p-b', userId: 'u-a' }],
      });
      const comment = { boardId: 'b-b1', cardId: 'c-b1', authorId: 'u-y2' };

      const result = await ensureSharedAccess({
        user,
        userGroupId: GROUP_A,
        models: world.models,
        runQuery: world.runQuery,
      });

      assert.deepStrictEqual(result.errors, []);
      assert.strictEqual(result.revoked, 0);
      assert.strictEqual(result.removedProjectManagers, 1);
      assertOnlyGroupAKept(world);
      assert.deepStrictEqual(notifiedOnComment(world, comment), ['u-y']);
    });
  });

  it('é idempotente: a segunda execução não altera nada', async () => {
    const world = makeWorld({
      projects: baseProjects,
      boards: baseBoards,
      boardGroups,
      boardMemberships: [
        { id: 'bm-old-b', boardId: 'b-b1', projectId: 'p-b', userId: 'u-a', role: 'editor' },
        { id: 'bm-viewer', boardId: 'b-a2', projectId: 'p-a', userId: 'u-a', role: 'viewer' },
      ],
      projectManagers: [
        { id: 'pm-a', projectId: 'p-a', userId: 'u-a' },
        { id: 'pm-b', projectId: 'p-b', userId: 'u-a' },
      ],
    });
    const run = () =>
      ensureSharedAccess({
        user,
        userGroupId: GROUP_A,
        models: world.models,
        runQuery: world.runQuery,
      });

    const first = await run();
    const snapshot = JSON.stringify(world.state);
    const second = await run();

    assert.ok(first.created + first.promoted + first.revoked + first.removedProjectManagers > 0);
    assert.deepStrictEqual(
      [second.created, second.promoted, second.revoked, second.removedProjectManagers],
      [0, 0, 0, 0],
    );
    assert.deepStrictEqual(second.errors, []);
    assert.strictEqual(JSON.stringify(world.state), snapshot);
  });

  it('mantém gerência de projeto criado pela UI (não espelhado) só com boards do grupo', async () => {
    const world = makeWorld({
      projects: [{ id: 'p-ui', ownerProjectManagerId: null }],
      boards: [{ id: 'b-ui', projectId: 'p-ui', creatorUserId: 'u-a' }],
      sameGroupUserIds: ['u-a'],
      groupedUserIds: ['u-a'],
      projectManagers: [{ id: 'pm-ui', projectId: 'p-ui', userId: 'u-a' }],
    });

    const result = await ensureSharedAccess({
      user,
      userGroupId: GROUP_A,
      models: world.models,
      runQuery: world.runQuery,
    });

    assert.strictEqual(result.removedProjectManagers, 0);
    assert.strictEqual(world.state.projectManagers.length, 1);
  });

  it('nunca remove gerência do admin interno (DEFAULT_ADMIN_EMAIL / INTERNAL)', async () => {
    const admin = { id: 'admin-internal', email: 'Admin@Mem0.local' };
    const world = makeWorld({
      projects: baseProjects,
      boards: baseBoards,
      boardGroups,
      mappedProjectIds: ['p-a', 'p-b'],
      projectManagers: [
        { id: 'pm-admin-a', projectId: 'p-a', userId: 'admin-internal' },
        { id: 'pm-admin-b', projectId: 'p-b', userId: 'admin-internal' },
      ],
    });

    const result = await ensureSharedAccess({
      user: admin,
      userGroupId: GROUP_A,
      models: world.models,
      runQuery: world.runQuery,
      internalAdminEmail: 'admin@mem0.local',
    });

    assert.strictEqual(result.removedProjectManagers, 0);
    assert.strictEqual(world.state.projectManagers.length, 2);
  });

  it('não toca gerência de projeto privado (fora de getShared)', async () => {
    const world = makeWorld({
      projects: [...baseProjects, { id: 'p-priv', ownerProjectManagerId: 'pm-priv' }],
      boards: baseBoards,
      boardGroups,
      mappedProjectIds: ['p-priv'],
      projectManagers: [{ id: 'pm-priv', projectId: 'p-priv', userId: 'u-a' }],
    });

    await ensureSharedAccess({
      user,
      userGroupId: GROUP_A,
      models: world.models,
      runQuery: world.runQuery,
    });

    assert.strictEqual(world.state.projectManagers.length, 1);
  });

  it('isola falhas: erro em um board/gerência não impede os demais passos', async () => {
    const world = makeWorld({
      projects: baseProjects,
      boards: baseBoards,
      boardGroups,
      mappedProjectIds: ['p-a', 'p-b'],
      boardMemberships: [
        { id: 'bm-old-b', boardId: 'b-b1', projectId: 'p-b', userId: 'u-a', role: 'editor' },
      ],
      projectManagers: [
        { id: 'pm-a', projectId: 'p-a', userId: 'u-a' },
        { id: 'pm-b', projectId: 'p-b', userId: 'u-a' },
      ],
      failures: { 'bm-create:b-a1': true, 'pm-delete:pm-b': true },
    });

    const result = await ensureSharedAccess({
      user,
      userGroupId: GROUP_A,
      models: world.models,
      runQuery: world.runQuery,
    });

    assert.strictEqual(result.errors.length, 2);
    // b-a2 criado e b-b1 revogado apesar das falhas; pm-a mantida (só grupo A).
    assert.deepStrictEqual(
      world.state.boardMemberships.map(({ boardId }) => boardId),
      ['b-a2'],
    );
    assert.deepStrictEqual(
      world.state.projectManagers.map(({ id }) => id),
      ['pm-a', 'pm-b'],
    );
  });

  it('falha na resolução de grupos não concede nem revoga nada', async () => {
    const world = makeWorld({
      projects: baseProjects,
      boards: baseBoards,
      boardMemberships: [
        { id: 'bm-a1', boardId: 'b-a1', projectId: 'p-a', userId: 'u-a', role: 'editor' },
      ],
      projectManagers: [{ id: 'pm-a', projectId: 'p-a', userId: 'u-a' }],
    });
    const failingQuery = async () => {
      throw new Error('pg down');
    };

    const result = await ensureSharedAccess({
      user,
      userGroupId: GROUP_A,
      models: world.models,
      runQuery: failingQuery,
    });

    assert.strictEqual(result.errors.length, 1);
    assert.strictEqual(world.state.boardMemberships.length, 1);
    assert.strictEqual(world.state.projectManagers.length, 1);
  });

  it("modo legado LAN (group '*') concede todos os boards e preserva gerências", async () => {
    const world = makeWorld({
      projects: baseProjects,
      boards: baseBoards,
      boardGroups,
      mappedProjectIds: ['p-a', 'p-b'],
      projectManagers: [{ id: 'pm-a', projectId: 'p-a', userId: 'u-a' }],
    });

    const result = await ensureSharedAccess({
      user,
      userGroupId: LEGACY_SHARED_GROUP,
      models: world.models,
      runQuery: world.runQuery,
    });

    assert.deepStrictEqual(result.errors, []);
    assert.strictEqual(result.created, 3);
    assert.strictEqual(world.state.projectManagers.length, 1);
  });

  it('sem usuário ou grupo é no-op', async () => {
    const world = makeWorld({ projects: baseProjects, boards: baseBoards });
    const result = await ensureSharedAccess({
      user,
      userGroupId: null,
      models: world.models,
      runQuery: world.runQuery,
    });
    assert.strictEqual(result.skipped, true);
  });
});

describe('mem0-shared-access isInternalAdminUser', () => {
  it('reconhece por e-mail (case-insensitive) e por id INTERNAL', () => {
    assert.strictEqual(
      isInternalAdminUser(
        { id: '1', email: 'ADMIN@mem0.local' },
        { internalAdminEmail: 'admin@mem0.local' },
      ),
      true,
    );
    assert.strictEqual(isInternalAdminUser({ id: 'int' }, { internalUserId: 'int' }), true);
    assert.strictEqual(
      isInternalAdminUser({ id: '1', email: 'p@x.com' }, { internalAdminEmail: '' }),
      false,
    );
  });
});
