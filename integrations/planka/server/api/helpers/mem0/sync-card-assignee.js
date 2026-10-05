/*!
 * Mem0 Shared — project Spec assignee onto PLANKA card_membership (single member).
 *
 * Uses CardMembership.qm directly (no Action/Notification side-effects) so the
 * Spec mirror stays reliable when search_path/notifications misbehave.
 */

module.exports = {
  friendlyName: 'Sync card assignee',

  description:
    'Ensures the card has exactly one membership for the Spec assignee (or none if cleared).',

  inputs: {
    cardId: { type: 'string', required: true },
    email: { type: 'string', allowNull: true },
    name: { type: 'string', allowNull: true },
    picture: { type: 'string', allowNull: true },
    actorUser: { type: 'ref', required: true },
    request: { type: 'ref' },
  },

  exits: {
    success: { description: 'Card memberships synced.' },
    cardNotFound: { description: 'Card not found.' },
  },

  async fn(inputs, exits) {
    const path = await sails.helpers.cards
      .getPathToProjectById(inputs.cardId)
      .tolerate('pathNotFound', () => null);

    if (!path) {
      return exits.cardNotFound();
    }

    const { card, board, project } = path;
    const existing = await CardMembership.qm.getByCardId(card.id);

    const broadcastDelete = (membership) => {
      sails.sockets.broadcast(
        `board:${board.id}`,
        'cardMembershipDelete',
        { item: membership },
        inputs.request,
      );
    };

    const broadcastCreate = (membership) => {
      sails.sockets.broadcast(
        `board:${board.id}`,
        'cardMembershipCreate',
        { item: membership },
        inputs.request,
      );
    };

    const rawEmail = inputs.email == null ? '' : String(inputs.email).trim();
    if (!rawEmail) {
      // eslint-disable-next-line no-restricted-syntax
      for (const membership of existing || []) {
        // eslint-disable-next-line no-await-in-loop
        const deleted = await CardMembership.qm.deleteOne(membership.id);
        if (deleted) broadcastDelete(deleted);
      }
      return exits.success({ cleared: true, userId: null });
    }

    const user = await sails.helpers.mem0.upsertUserByEmail.with({
      email: rawEmail,
      name: inputs.name,
      picture: inputs.picture,
      actorUser: inputs.actorUser,
    });

    // Card membership requires board membership (no admin bypass). Só board
    // membership EDITOR: gerência de projeto vazaria boards de outros grupos.
    let boardMembership = await BoardMembership.qm.getOneByBoardIdAndUserId(board.id, user.id);
    if (!boardMembership) {
      boardMembership = await BoardMembership.qm.createOne({
        projectId: project.id,
        boardId: board.id,
        userId: user.id,
        role: BoardMembership.Roles.EDITOR,
      });
    } else if (boardMembership.role !== BoardMembership.Roles.EDITOR) {
      boardMembership = await BoardMembership.qm.updateOne(boardMembership.id, {
        role: BoardMembership.Roles.EDITOR,
      });
    }

    // eslint-disable-next-line no-restricted-syntax
    for (const membership of existing || []) {
      if (membership.userId !== user.id) {
        // eslint-disable-next-line no-await-in-loop
        const deleted = await CardMembership.qm.deleteOne(membership.id);
        if (deleted) broadcastDelete(deleted);
      }
    }

    const already = (existing || []).some((m) => m.userId === user.id);
    if (!already) {
      try {
        const created = await CardMembership.qm.createOne({
          cardId: card.id,
          userId: user.id,
        });
        broadcastCreate(created);
      } catch (error) {
        if (error.code !== 'E_UNIQUE') {
          throw error;
        }
      }
    }

    return exits.success({ cleared: false, userId: user.id, email: user.email });
  },
};
