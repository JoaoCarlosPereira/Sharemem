/*!
 * Copyright (c) 2024 PLANKA Software GmbH
 * Licensed under the Fair Use License: https://github.com/plankanban/planka/blob/master/LICENSE.md
 */

/**
 * @swagger
 * /projects/{id}:
 *   get:
 *     summary: Get project details
 *     description: Retrieves comprehensive project information, including boards, board memberships, and other related data.
 *     tags:
 *       - Projects
 *     operationId: getProject
 *     parameters:
 *       - name: id
 *         in: path
 *         required: true
 *         description: ID of the project to retrieve
 *         schema:
 *           type: string
 *           example: "1357158568008091264"
 *     responses:
 *       200:
 *         description: Project details retrieved successfully
 *         content:
 *           application/json:
 *             schema:
 *               type: object
 *               required:
 *                 - item
 *                 - included
 *               properties:
 *                 item:
 *                   allOf:
 *                     - $ref: '#/components/schemas/Project'
 *                     - type: object
 *                       properties:
 *                         isFavorite:
 *                           type: boolean
 *                           description: Whether the project is marked as favorite by the current user
 *                           example: true
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
 *       404:
 *         $ref: '#/components/responses/NotFound'
 */

const { idInput } = require('../../../utils/inputs');
const filterBoardsByGroup = require('../../../utils/filter-boards-by-group');
const getBoardGroupIds = require('../../../utils/get-board-group-ids');
const getGroupVisibilityUserIds = require('../../../utils/get-group-visibility-user-ids');
const filterProjectsByVisibleBoards = require('../../../utils/filter-projects-by-visible-boards');
const { getMappedProjectIds } = require('../../../utils/mem0-shared-access');
const { hasAdminAccessToSharedProject } = require('../../../utils/mem0-group-scope');

const Errors = {
  PROJECT_NOT_FOUND: {
    projectNotFound: 'Project not found',
  },
};

module.exports = {
  inputs: {
    id: {
      ...idInput,
      required: true,
    },
  },

  exits: {
    projectNotFound: {
      responseType: 'notFound',
    },
  },

  async fn(inputs) {
    const { currentUser } = this.req;

    const project = await Project.qm.getOneById(inputs.id);

    if (!project) {
      throw Errors.PROJECT_NOT_FOUND;
    }

    const isProjectManager = await sails.helpers.users.isProjectManager(currentUser.id, project.id);

    const boardMemberships = await BoardMembership.qm.getByProjectIdAndUserId(
      project.id,
      currentUser.id,
    );

    let boards;
    if (!hasAdminAccessToSharedProject(this.req, project)) {
      if (!isProjectManager) {
        if (boardMemberships.length === 0) {
          throw Errors.PROJECT_NOT_FOUND; // Forbidden
        }

        const boardIds = sails.helpers.utils.mapRecords(boardMemberships, 'boardId');
        boards = await Board.qm.getByIds(boardIds);
      }
    }

    if (!boards) {
      boards = await Board.qm.getByProjectId(project.id);
    }

    const legacySharedMode = this.req.mem0Auth && this.req.mem0Auth.group === '*';
    if (currentUser.email && !legacySharedMode) {
      const runQuery = (sql, values) => sails.sendNativeQuery(sql, values);
      const allBoards = boards;
      let isVisible = false;
      try {
        const groupVisibilityUserIds = await getGroupVisibilityUserIds(
          runQuery,
          currentUser,
          this.req.mem0Auth && this.req.mem0Auth.group,
        );
        if (groupVisibilityUserIds) {
          const boardGroupIds = await getBoardGroupIds(runQuery, boards);
          boards = filterBoardsByGroup(
            boards,
            [...groupVisibilityUserIds.sameGroupUserIds, currentUser.id],
            groupVisibilityUserIds.groupedUserIds,
            {
              boardGroupIds,
              currentGroupId: groupVisibilityUserIds.currentGroupId,
              restrictUnknownCreators: groupVisibilityUserIds.restrictUnknownCreators,
            },
          );
          // Mem0 Shared: projeto sem board visível = 404 (não vaza nome/gerentes).
          const mappedProjectIds = await getMappedProjectIds(runQuery, [project.id]);
          const managers = await ProjectManager.qm.getByProjectId(project.id);
          isVisible =
            filterProjectsByVisibleBoards([project], {
              allBoards,
              visibleBoards: boards,
              currentUserId: currentUser.id,
              managerProjectIds: isProjectManager ? [project.id] : [],
              projectManagers: managers,
              sameGroupUserIds: groupVisibilityUserIds.sameGroupUserIds,
              groupedUserIds: groupVisibilityUserIds.groupedUserIds,
              mappedProjectIds,
            }).length > 0;
        } else {
          isVisible = true;
        }
      } catch (error) {
        sails.log.warn('projects/show: failed to resolve current user group:', error.message);
        isVisible = false;
      }

      if (!isVisible) {
        throw Errors.PROJECT_NOT_FOUND;
      }
    }

    const visibleBoardIds = new Set(boards.map(({ id }) => String(id)));
    const visibleBoardMemberships = boardMemberships.filter(({ boardId }) =>
      visibleBoardIds.has(String(boardId)),
    );

    project.isFavorite = await sails.helpers.users.isProjectFavorite(currentUser.id, project.id);

    const projectManagers = await ProjectManager.qm.getByProjectId(project.id);

    const userIds = sails.helpers.utils.mapRecords(projectManagers, 'userId');
    const users = await User.qm.getByIds(userIds);

    const backgroundImages = await BackgroundImage.qm.getByProjectId(project.id);

    const baseCustomFieldGroups = await BaseCustomFieldGroup.qm.getByProjectId(project.id);
    const baseCustomFieldGroupsIds = sails.helpers.utils.mapRecords(baseCustomFieldGroups);

    const customFields =
      await CustomField.qm.getByBaseCustomFieldGroupIds(baseCustomFieldGroupsIds);

    let notificationServices = [];
    if (isProjectManager) {
      boardIds = sails.helpers.utils.mapRecords(boards);
      notificationServices = await NotificationService.qm.getByBoardIds(boardIds);
    }

    return {
      item: project,
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
