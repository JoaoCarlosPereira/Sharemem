/*!
 * Mem0 Shared — no modo isolado por grupo, ser ADMIN não dá visão total de
 * projeto compartilhado (no ShareMem toda pessoa do embed é ADMIN).
 */

const filterBoardsByGroup = require('./filter-boards-by-group');
const getBoardGroupIds = require('./get-board-group-ids');
const getGroupVisibilityUserIds = require('./get-group-visibility-user-ids');
const { LEGACY_SHARED_GROUP } = require('./mem0-shared-access');

const isMem0BridgeActive = (env = process.env) => Boolean(String(env.AUTH_JWT_SECRET || '').trim());

const isLegacySharedRequest = (req) =>
  Boolean(req && req.mem0Auth && String(req.mem0Auth.group) === LEGACY_SHARED_GROUP);

// Substitui o atalho upstream `role === ADMIN && !project.ownerProjectManagerId`.
// Fail-closed: com a ponte ativa, o atalho só vale no modo legado LAN
// (group '*'). Sem `req.mem0Auth` (cookie de /attachments, socket sem JWT,
// bearer internal/omtk/legacy) não há atalho — vale gerência/membership.
const hasAdminAccessToSharedProject = (req, project) =>
  Boolean(req && req.currentUser) &&
  req.currentUser.role === User.Roles.ADMIN &&
  !(project && project.ownerProjectManagerId) &&
  (!isMem0BridgeActive() || isLegacySharedRequest(req));

// Board memberships do usuário no projeto, só em boards visíveis ao grupo
// dele. Sem ponte (ou modo legado) = todas. Erro ao resolver grupo = [].
const getGroupVisibleBoardMemberships = async (req, projectId) => {
  const { currentUser } = req;
  const memberships = await BoardMembership.qm.getByProjectIdAndUserId(projectId, currentUser.id);

  if (memberships.length === 0 || !isMem0BridgeActive() || isLegacySharedRequest(req)) {
    return memberships;
  }

  const runQuery = (sql, values) => sails.sendNativeQuery(sql, values);
  try {
    const visibility = await getGroupVisibilityUserIds(
      runQuery,
      currentUser,
      req.mem0Auth && req.mem0Auth.group,
    );
    if (!visibility) {
      return [];
    }

    const boards = await Board.qm.getByIds(memberships.map(({ boardId }) => boardId));
    const visibleBoardIds = new Set(
      filterBoardsByGroup(
        boards,
        [...visibility.sameGroupUserIds, currentUser.id],
        visibility.groupedUserIds,
        {
          boardGroupIds: await getBoardGroupIds(runQuery, boards),
          currentGroupId: visibility.currentGroupId,
          restrictUnknownCreators: true,
        },
      ).map(({ id }) => String(id)),
    );

    return memberships.filter(({ boardId }) => visibleBoardIds.has(String(boardId)));
  } catch (error) {
    sails.log.warn('mem0-group-scope: failed to resolve current user group:', error.message);
    return [];
  }
};

module.exports = {
  getGroupVisibleBoardMemberships,
  hasAdminAccessToSharedProject,
  isLegacySharedRequest,
  isMem0BridgeActive,
};
