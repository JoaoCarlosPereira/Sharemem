/*!
 * Mem0 Shared — limpeza que acompanha a perda de acesso de um usuário a um board.
 *
 * Extraída de api/helpers/board-memberships/delete-one.js para ser reaproveitada
 * na reconciliação por grupo (utils/mem0-shared-access.js). Sem isso, quem perde
 * o acesso continua inscrito (BoardSubscription/CardSubscription) e segue
 * recebendo notificações (nome do card + texto do comentário) do board.
 *
 * Remove do usuário, no board: inscrição no board, inscrições e memberships nos
 * cards e o responsável das tasks. Idempotente.
 */

const revokeBoardAccessSideEffects = async ({ boardId, userId, models }) => {
  const { BoardSubscription, Card, CardSubscription, CardMembership, TaskList, Task } = models;

  await BoardSubscription.qm.delete({
    boardId,
    userId,
  });

  const cards = (await Card.qm.getByBoardId(boardId)) || [];
  const cardIds = cards.map(({ id }) => id);

  await CardSubscription.qm.delete({
    cardId: cardIds,
    userId,
  });

  await CardMembership.qm.delete({
    cardId: cardIds,
    userId,
  });

  const taskLists = (await TaskList.qm.getByCardIds(cardIds)) || [];
  const taskListIds = taskLists.map(({ id }) => id);

  await Task.qm.update(
    {
      taskListId: taskListIds,
      assigneeUserId: userId,
    },
    {
      assigneeUserId: null,
    },
  );
};

module.exports = revokeBoardAccessSideEffects;
