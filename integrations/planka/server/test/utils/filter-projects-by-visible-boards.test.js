const assert = require('assert');

const filterProjectsByVisibleBoards = require('../../utils/filter-projects-by-visible-boards');

describe('filterProjectsByVisibleBoards', () => {
  const projects = [
    { id: 'p-a', name: 'Projeto do grupo A' },
    { id: 'p-b', name: 'Projeto do grupo B' },
    { id: 'p-empty-mirror', name: 'Workspace sem board' },
    { id: 'p-ui-new', name: 'Criado agora pela UI' },
    { id: 'p-ui-foreign', name: 'Vazio de outro grupo' },
  ];
  const allBoards = [
    { id: 'b-a', projectId: 'p-a' },
    { id: 'b-b', projectId: 'p-b' },
  ];

  const run = (overrides = {}) =>
    filterProjectsByVisibleBoards(projects, {
      allBoards,
      visibleBoards: [{ id: 'b-a', projectId: 'p-a' }],
      currentUserId: 'u-a',
      managerProjectIds: ['p-ui-new', 'p-ui-foreign', 'p-empty-mirror'],
      projectManagers: [
        { projectId: 'p-ui-new', userId: 'u-a' },
        { projectId: 'p-ui-new', userId: 'admin-internal' },
        { projectId: 'p-ui-foreign', userId: 'u-a' },
        { projectId: 'p-ui-foreign', userId: 'u-b' },
        { projectId: 'p-empty-mirror', userId: 'u-a' },
      ],
      sameGroupUserIds: ['u-a'],
      groupedUserIds: ['u-a', 'u-b'],
      mappedProjectIds: new Set(['p-a', 'p-b', 'p-empty-mirror']),
      ...overrides,
    }).map(({ id }) => id);

  it('esconde projetos sem board visível (sem vazar nomes de outro grupo)', () => {
    const ids = run();
    assert.ok(ids.includes('p-a'));
    assert.ok(!ids.includes('p-b'));
    assert.ok(!ids.includes('p-empty-mirror'));
    assert.ok(!ids.includes('p-ui-foreign'));
  });

  it('mantém projeto recém-criado pela UI (vazio, não espelhado, gerentes do grupo)', () => {
    assert.ok(run().includes('p-ui-new'));
  });

  it('fail-closed: projeto vazio tratado como espelhado some', () => {
    const ids = run({ mappedProjectIds: new Set(projects.map(({ id }) => id)) });
    assert.deepStrictEqual(ids, ['p-a']);
  });
});
