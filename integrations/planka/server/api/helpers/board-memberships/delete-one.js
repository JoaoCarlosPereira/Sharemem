/*!
 * Copyright (c) 2024 PLANKA Software GmbH
 * Licensed under the Fair Use License: https://github.com/plankanban/planka/blob/master/LICENSE.md
 */

const { v4: uuid } = require('uuid');

const { isMem0BridgeActive } = require('../../../utils/mem0-group-scope');
const revokeBoardAccessSideEffects = require('../../../utils/revoke-board-access-side-effects');

module.exports = {
  inputs: {
    record: {
      type: 'ref',
      required: true,
    },
    user: {
      type: 'ref',
      required: true,
    },
    project: {
      type: 'ref',
      required: true,
    },
    board: {
      type: 'ref',
      required: true,
    },
    actorUser: {
      type: 'ref',
      required: true,
    },
    request: {
      type: 'ref',
    },
  },

  async fn(inputs) {
    // Mem0 Shared: limpeza extraída para reuso na reconciliação por grupo.
    await revokeBoardAccessSideEffects({
      boardId: inputs.record.boardId,
      userId: inputs.user.id,
      models: { BoardSubscription, Card, CardSubscription, CardMembership, TaskList, Task },
    });

    const boardMembership = await BoardMembership.qm.deleteOne(inputs.record.id);

    if (boardMembership) {
      // Mem0 Shared: com a ponte ativa ADMIN não tem visão total — expulsa da sala.
      if (
        inputs.user.role !== User.Roles.ADMIN ||
        inputs.project.ownerProjectManagerId ||
        isMem0BridgeActive()
      ) {
        const isProjectManager = await sails.helpers.users.isProjectManager(
          boardMembership.userId,
          inputs.project.id,
        );

        if (!isProjectManager) {
          sails.sockets.removeRoomMembersFromRooms(
            `@user:${boardMembership.userId}`,
            `board:${boardMembership.boardId}`,
          );
        }
      }

      sails.sockets.broadcast(
        `user:${boardMembership.userId}`,
        'boardMembershipDelete',
        {
          item: boardMembership,
        },
        inputs.request,
      );

      const tempRoom = uuid();

      sails.sockets.addRoomMembersToRooms(`board:${boardMembership.boardId}`, tempRoom, () => {
        sails.sockets.removeRoomMembersFromRooms(`user:${boardMembership.userId}`, tempRoom, () => {
          sails.sockets.broadcast(
            tempRoom,
            'boardMembershipDelete',
            {
              item: boardMembership,
            },
            inputs.request,
          );

          sails.sockets.removeRoomMembersFromRooms(tempRoom, tempRoom);
        });
      });

      const webhooks = await Webhook.qm.getAll();

      sails.helpers.utils.sendWebhooks.with({
        webhooks,
        event: Webhook.Events.BOARD_MEMBERSHIP_DELETE,
        buildData: () => ({
          item: boardMembership,
          included: {
            users: [sails.helpers.users.presentOne(inputs.user)],
            projects: [inputs.project],
            boards: [inputs.board],
          },
        }),
        user: inputs.actorUser,
      });
    }

    return boardMembership;
  },
};
