/*!
 * Mem0 Shared — esconde projetos sem board visível para o usuário (modo
 * isolado por grupo). Evita vazar o NOME de projetos de outros grupos, que o
 * PLANKA upstream lista inteiros para ADMIN (getShared) ou por gerência antiga.
 *
 * Um projeto é visível quando:
 *   1. contém ao menos um board visível (já filtrado por grupo); ou
 *   2. exceção de criação pela UI: o projeto NÃO tem board nenhum, NÃO é
 *      espelho de workspace ShareMem (spec_planka_id_map 'project'), o usuário
 *      é gerente e todos os gerentes humanos são do mesmo grupo dele (usuários
 *      técnicos sem grupo, ex. admin@mem0.local, são ignorados). Sem isso, um
 *      projeto recém-criado pela UI some antes de receber o primeiro board.
 */

module.exports = (
  projects,
  {
    allBoards = [],
    visibleBoards = [],
    currentUserId = null,
    managerProjectIds = [],
    projectManagers = [],
    sameGroupUserIds = [],
    groupedUserIds = [],
    mappedProjectIds = new Set(),
  } = {},
) => {
  const visibleBoardProjectIds = new Set(visibleBoards.map(({ projectId }) => String(projectId)));
  const projectIdsWithAnyBoard = new Set(allBoards.map(({ projectId }) => String(projectId)));
  const managerProjectIdSet = new Set(managerProjectIds.map(String));
  const sameGroupSet = new Set([...sameGroupUserIds.map(String), String(currentUserId)]);
  const groupedSet = new Set(groupedUserIds.map(String));

  const managerUserIdsByProjectId = projectManagers.reduce((result, { projectId, userId }) => {
    const key = String(projectId);
    // eslint-disable-next-line no-param-reassign
    result[key] = [...(result[key] || []), String(userId)];
    return result;
  }, {});

  const isOwnEmptyProject = (projectId) => {
    if (!managerProjectIdSet.has(projectId)) return false;
    if (projectIdsWithAnyBoard.has(projectId)) return false;
    if (mappedProjectIds.has(projectId)) return false;
    return (managerUserIdsByProjectId[projectId] || []).every(
      (userId) => sameGroupSet.has(userId) || !groupedSet.has(userId),
    );
  };

  return projects.filter((project) => {
    const projectId = String(project.id);
    return visibleBoardProjectIds.has(projectId) || isOwnEmptyProject(projectId);
  });
};
