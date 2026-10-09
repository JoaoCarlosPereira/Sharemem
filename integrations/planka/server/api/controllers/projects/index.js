/*!
 * Copyright (c) 2024 PLANKA Software GmbH
 * Licensed under the Fair Use License: https://github.com/plankanban/planka/blob/master/LICENSE.md
 */

/**
 * @swagger
 * /projects:
 *   get:
 *     summary: Get all accessible projects
 *     description: Retrieves all projects the current user has access to, including managed projects, membership projects, and shared projects (for admins).
 *     tags:
 *       - Projects
 *     operationId: getProjects
 *     responses:
 *       200:
 *         description: Projects retrieved successfully
 *         content:
 *           application/json:
 *             schema:
 *               type: object
 *               required:
 *                 - items
 *                 - included
 *               properties:
 *                 items:
 *                   type: array
 *                   items:
 *                     allOf:
 *                       - $ref: '#/components/schemas/Project'
 *                       - type: object
 *                         properties:
 *                           isFavorite:
 *                             type: boolean
 *                             description: Whether the project is marked as favorite by the current user
 *                             example: true
 *                 included:
 *                   type: object
 *                   required:
 *                     - users
 *                     - projectManagers
 *                     - backgroundImages
 *                     - baseCustomFieldGroups
 *                     - boards
 *                     - boardMemberships
 *                     - customFields
 *                     - notificationServices
 *                   properties:
 *                     users:
 *                       type: array
 *                       description: Related users
 *                       items:
 *                         $ref: '#/components/schemas/User'
 *                     projectManagers:
 *                       type: array
 *                       description: Related project managers
 *                       items:
 *                         $ref: '#/components/schemas/ProjectManager'
 *                     backgroundImages:
 *                       type: array
 *                       description: Related background images
 *                       items:
 *                         $ref: '#/components/schemas/BackgroundImage'
 *                     baseCustomFieldGroups:
 *                       type: array
 *                       description: Related base custom field groups
 *                       items:
 *                         $ref: '#/components/schemas/BaseCustomFieldGroup'
 *                     boards:
 *                       type: array
 *                       description: Related boards
 *                       items:
 *                         $ref: '#/components/schemas/Board'
 *                     boardMemberships:
 *                       type: array
 *                       description: Related board memberships (for current user)
 *                       items:
 *                         $ref: '#/components/schemas/BoardMembership'
 *                     customFields:
 *                       type: array
 *                       description: Related custom fields
 *                       items:
 *                         $ref: '#/components/schemas/CustomField'
 *                     notificationServices:
 *                       type: array
 *                       description: Related notification services (for managed projects)
 *                       items:
 *                         $ref: '#/components/schemas/NotificationService'
 *       400:
 *         $ref: '#/components/responses/ValidationError'
 *       401:
 *         $ref: '#/components/responses/Unauthorized'
 */

const filterBoardsByGroup = require('../../../utils/filter-boards-by-group');
const getBoardGroupIds = require('../../../utils/get-board-group-ids');
const getGroupVisibilityUserIds = require('../../../utils/get-group-visibility-user-ids');
const filterProjectsByVisibleBoards = require('../../../utils/filter-projects-by-visible-boards');
const { getMappedProjectIds } = require('../../../utils/mem0-shared-access');
const { hasAdminAccessToSharedProject } = require('../../../utils/mem0-group-scope');

const EMPTY_RESPONSE = () => ({
  items: [],
  included: {
    projectManagers: [],
    baseCustomFieldGroups: [],
    boards: [],
    boardMemberships: [],
    customFields: [],
    notificationServices: [],
    users: [],
    backgroundImages: [],
  },
});

module.exports = {
  async fn() {
    const { currentUser } = this.req;
    const legacySharedMode = this.req.mem0Auth && this.req.mem0Auth.group === '*';

    let groupVisibilityUserIds;
    if (!legacySharedMode) {
      try {
        groupVisibilityUserIds = await getGroupVisibilityUserIds(
          (sql, values) => sails.sendNativeQuery(sql, values),
          currentUser,
          this.req.mem0Auth && this.req.mem0Auth.group,
        );
      } catch (error) {
        // Fail-closed: sem grupo resolvido, nenhum projeto.
        sails.log.warn('projects/index: failed to resolve current user group:', error.message);
        return EMPTY_RESPONSE();
      }
    }

    let boardGroupIds = {};

    let sharedProjects;
    let sharedProjectIds;

    let managerProjectIds = await sails.helpers.users.getManagerProjectIds(currentUser.id);
    const fullyVisibleProjectIds = [...managerProjectIds];

    if (hasAdminAccessToSharedProject(this.req, null)) {
      sharedProjects = await Project.qm.getShared({
        exceptIdOrIds: managerProjectIds,
      });

      sharedProjectIds = sails.helpers.utils.mapRecords(sharedProjects);
      fullyVisibleProjectIds.push(...sharedProjectIds);
    }

    const boardMemberships = await BoardMembership.qm.getByUserId(currentUser.id);
    const membershipBoardIds = sails.helpers.utils.mapRecords(boardMemberships, 'boardId');

    const membershipBoards = await Board.qm.getByIds(membershipBoardIds, {
      exceptProjectIdOrIds: fullyVisibleProjectIds,
    });

    const membershipProjectIds = sails.helpers.utils.mapRecords(
      membershipBoards,
      'projectId',
      true,
    );

    let projectIds = [...managerProjectIds, ...membershipProjectIds];
    let projects = await Project.qm.getByIds(projectIds);

    if (sharedProjectIds) {
      projectIds.push(...sharedProjectIds);
      projects.push(...sharedProjects);
    }

    const fullyVisibleBoards = await Board.qm.getByProjectIds(fullyVisibleProjectIds);
    const allBoards = [...fullyVisibleBoards, ...membershipBoards];
    let boards = allBoards;

    if (groupVisibilityUserIds) {
      try {
        boardGroupIds = await getBoardGroupIds(
          (sql, values) => sails.sendNativeQuery(sql, values),
          boards,
        );
      } catch (error) {
        sails.log.warn('projects/index: failed to resolve board groups:', error.message);
        groupVisibilityUserIds.restrictUnknownCreators = true;
      }

      boards = filterBoardsByGroup(
        boards,
        [...groupVisibilityUserIds.sameGroupUserIds, currentUser.id],
        groupVisibilityUserIds.groupedUserIds,
        {
          restrictUnknownCreators: groupVisibilityUserIds.restrictUnknownCreators,
          boardGroupIds,
          currentGroupId: groupVisibilityUserIds.currentGroupId,
        },
      );
    }

    const visibleBoardIds = new Set(boards.map(({ id }) => String(id)));
    const visibleBoardMemberships = boardMemberships.filter(({ boardId }) =>
      visibleBoardIds.has(String(boardId)),
    );

    let projectManagers = await ProjectManager.qm.getByProjectIds(projectIds);

    if (groupVisibilityUserIds) {
      // Mem0 Shared: sem bypass de ADMIN — projeto sem board visível some
      // (inclusive nome), e o restante do payload acompanha o recorte.
      let mappedProjectIds = new Set();
      try {
        mappedProjectIds = await getMappedProjectIds(
          (sql, values) => sails.sendNativeQuery(sql, values),
          projectIds,
        );
      } catch (error) {
        sails.log.warn('projects/index: failed to resolve mirrored projects:', error.message);
        // Fail-closed: tratar todos como espelhados (só aparecem com board visível).
        mappedProjectIds = new Set(projectIds.map(String));
      }

      const visibleProjects = filterProjectsByVisibleBoards(projects, {
        allBoards,
        visibleBoards: boards,
        currentUserId: currentUser.id,
        managerProjectIds,
        projectManagers,
        sameGroupUserIds: groupVisibilityUserIds.sameGroupUserIds,
        groupedUserIds: groupVisibilityUserIds.groupedUserIds,
        mappedProjectIds,
      });
      const visibleProjectIds = new Set(visibleProjects.map(({ id }) => String(id)));

      projects = visibleProjects;
      projectIds = projectIds.filter((id) => visibleProjectIds.has(String(id)));
      managerProjectIds = managerProjectIds.filter((id) => visibleProjectIds.has(String(id)));
      projectManagers = projectManagers.filter(({ projectId }) =>
        visibleProjectIds.has(String(projectId)),
      );
    }

    const projectFavorites = await ProjectFavorite.qm.getByProjectIdsAndUserId(
      projectIds,
      currentUser.id,
    );

    const userIds = sails.helpers.utils.mapRecords(projectManagers, 'userId', true);
    const users = await User.qm.getByIds(userIds);

    const backgroundImages = await BackgroundImage.qm.getByProjectIds(projectIds);

    const baseCustomFieldGroups = await BaseCustomFieldGroup.qm.getByProjectIds(projectIds);
    const baseCustomFieldGroupsIds = sails.helpers.utils.mapRecords(baseCustomFieldGroups);

    const customFields =
      await CustomField.qm.getByBaseCustomFieldGroupIds(baseCustomFieldGroupsIds);

    let notificationServices = [];
    if (managerProjectIds.length > 0) {
      const managerProjectIdsSet = new Set(managerProjectIds);

      const managerBoardIds = boards.flatMap((board) =>
        managerProjectIdsSet.has(board.projectId) ? board.id : [],
      );

      notificationServices = await NotificationService.qm.getByBoardIds(managerBoardIds);
    }

    const isFavoriteByProjectId = projectFavorites.reduce(
      (result, projectFavorite) => ({
        ...result,
        [projectFavorite.projectId]: true,
      }),
      {},
    );

    projects.forEach((project) => {
      // eslint-disable-next-line no-param-reassign
      project.isFavorite = isFavoriteByProjectId[project.id] || false;
    });

    return {
      items: projects,
      included: {
        projectManagers,
        baseCustomFieldGroups,
        boards,
        boardMemberships: visibleBoardMemberships,
        customFields,
        notificationServices,
        users: sails.helpers.users.presentMany(users, currentUser),
        backgroundImages: sails.helpers.backgroundImages.presentMany(backgroundImages),
      },
    };
  },
};
