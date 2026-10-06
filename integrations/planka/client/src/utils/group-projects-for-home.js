import { ProjectGroups } from '../constants/Enums';

/**
 * Agrupa os projetos da home. Com `isMem0Shared` (servidor já recorta por
 * grupo) tudo vai para "Equipe" — sem "Compartilhados comigo"/"Outros".
 */
export default (
  { managerProjectModels = [], membershipProjectModels = [], adminProjectModels = [] },
  { isMem0Shared = false } = {},
) => {
  const result = {
    [ProjectGroups.MY_OWN]: [],
    [ProjectGroups.TEAM]: [],
    [ProjectGroups.SHARED_WITH_ME]: [],
    [ProjectGroups.OTHERS]: [],
    teamActiveIds: [],
    teamCompletedIds: [],
    teamArchivedIds: [],
  };

  const pushTeam = (projectModel) => {
    result[ProjectGroups.TEAM].push(projectModel.id);

    if (projectModel.isArchived) {
      result.teamArchivedIds.push(projectModel.id);
    } else if (projectModel.isCompleted) {
      result.teamCompletedIds.push(projectModel.id);
    } else {
      result.teamActiveIds.push(projectModel.id);
    }
  };

  const pushOwnedOrTeam = (projectModel) => {
    if (projectModel.ownerProjectManager) {
      result[ProjectGroups.MY_OWN].push(projectModel.id);
    } else {
      pushTeam(projectModel);
    }
  };

  managerProjectModels.forEach(pushOwnedOrTeam);

  if (isMem0Shared) {
    membershipProjectModels.forEach(pushOwnedOrTeam);
    adminProjectModels.forEach(pushOwnedOrTeam);
  } else {
    result[ProjectGroups.SHARED_WITH_ME] = membershipProjectModels.map(({ id }) => id);
    result[ProjectGroups.OTHERS] = adminProjectModels.map(({ id }) => id);
  }

  return result;
};
