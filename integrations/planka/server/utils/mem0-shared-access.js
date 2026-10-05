/*!
 * Mem0 Shared — reconcilia o acesso de um usuário JWT (embed ShareMem) aos
 * projetos/boards compartilhados do PLANKA a partir do GRUPO do usuário.
 *
 *   - Board do grupo → board membership EDITOR (cria ou promove).
 *   - Board de outro grupo → revoga a membership e tira o usuário da sala
 *     `board:<id>` (sem isso, o socket já aberto seguiria recebendo eventos).
 *     Também limpa inscrições/memberships de card/responsável de task
 *     (revoke-board-access-side-effects), senão as notificações vazam.
 *   - Gerência de projeto: mantida quando TODOS os boards do projeto são do
 *     grupo (preserva criar board pela UI); removida quando há board de outro
 *     grupo. Board sem mapeamento criado pelo admin interno (ou sem criador)
 *     conta como "de outro grupo" (fail-closed) — inclusive na janela entre
 *     criar o board espelhado e gravar `spec_planka_id_map`: a membership
 *     volta no próximo ciclo de TTL, a gerência não. Nunca toca gerência do
 *     admin interno nem a dona de projeto privado.
 *   - Modo legado LAN (group '*'): todo board recebe EDITOR; gerências intactas.
 *
 * Mesma decisão de visibilidade do caminho de leitura (filterBoardsByGroup +
 * getBoardGroupIds + getGroupVisibilityUserIds). Cada passo tem try/catch
 * próprio. Idempotente: a 2ª execução não altera nada.
 */

const filterBoardsByGroup = require('./filter-boards-by-group');
const getBoardGroupIds = require('./get-board-group-ids');
const getGroupVisibilityUserIds = require('./get-group-visibility-user-ids');
const revokeBoardAccessSideEffects = require('./revoke-board-access-side-effects');

const LEGACY_SHARED_GROUP = '*';

const errorMessage = (error) => (error && error.message) || String(error);

const isInternalAdminUser = (user, { internalAdminEmail, internalUserId } = {}) => {
  if (!user) return false;
  if (internalUserId && String(user.id) === String(internalUserId)) return true;
  const email = String(user.email || '')
    .trim()
    .toLowerCase();
  const adminEmail = String(internalAdminEmail || '')
    .trim()
    .toLowerCase();
  return Boolean(adminEmail) && email === adminEmail;
};

const getMappedProjectIds = async (runQuery, projectIds) => {
  if (projectIds.length === 0) return new Set();
  const result = await runQuery(
    `SELECT mapping.planka_id AS project_id
       FROM public.spec_planka_id_map AS mapping
      WHERE mapping.entity_type = 'project'
        AND mapping.planka_id = ANY($1::text[])`,
    [projectIds.map(String)],
  );
  return new Set(result.rows.map((row) => String(row.project_id)));
};

/**
 * @returns {Promise<{created: number, promoted: number, revoked: number,
 *   removedProjectManagers: number, skipped: boolean, errors: string[]}>}
 */
const ensureSharedAccess = async ({
  user,
  userGroupId,
  models,
  runQuery,
  internalAdminEmail,
  internalUserId,
  sockets = null,
}) => {
  const result = {
    created: 0,
    promoted: 0,
    revoked: 0,
    removedProjectManagers: 0,
    skipped: false,
    errors: [],
  };

  if (!user || !user.id || !userGroupId) {
    result.skipped = true;
    return result;
  }

  const { Project, Board, BoardMembership, ProjectManager } = models;
  const leaveBoardRoom = (boardId) => {
    if (sockets) sockets.removeRoomMembersFromRooms(`@user:${user.id}`, `board:${boardId}`);
  };
  const legacy = String(userGroupId) === LEGACY_SHARED_GROUP;

  let projects;
  try {
    projects = (await Project.qm.getShared()) || [];
  } catch (error) {
    result.errors.push(`projects: ${errorMessage(error)}`);
    return result;
  }

  // Boards de cada projeto compartilhado (falha de um projeto não para os demais).
  const boards = [];
  const projectById = new Map();
  // eslint-disable-next-line no-restricted-syntax
  for (const project of projects) {
    projectById.set(String(project.id), project);
    try {
      // eslint-disable-next-line no-await-in-loop
      const projectBoards = (await Board.qm.getByProjectIds([project.id])) || [];
      boards.push(...projectBoards);
    } catch (error) {
      result.errors.push(`boards(${project.id}): ${errorMessage(error)}`);
    }
  }

  let isVisible;
  if (legacy) {
    isVisible = () => true;
  } else {
    let visibility;
    let boardGroupIds;
    try {
      visibility = await getGroupVisibilityUserIds(runQuery, user, userGroupId);
      boardGroupIds = await getBoardGroupIds(runQuery, boards);
    } catch (error) {
      // Sem resolver grupos não dá para decidir nada com segurança: não concede
      // nem revoga nada nesta rodada (o caminho de leitura continua fail-closed).
      result.errors.push(`group-resolution: ${errorMessage(error)}`);
      return result;
    }
    if (!visibility) {
      result.skipped = true;
      return result;
    }
    const visibleIds = new Set(
      filterBoardsByGroup(
        boards,
        [...visibility.sameGroupUserIds, user.id],
        visibility.groupedUserIds,
        {
          restrictUnknownCreators: true,
          boardGroupIds,
          currentGroupId: visibility.currentGroupId,
        },
      ).map(({ id }) => String(id)),
    );
    isVisible = (board) => visibleIds.has(String(board.id));
  }

  const foreignBoardIdsByProjectId = new Map();

  // 1) Board memberships primeiro (garante o acesso novo antes de tirar o antigo).
  // eslint-disable-next-line no-restricted-syntax
  for (const board of boards) {
    const visible = isVisible(board);
    if (!visible) {
      const key = String(board.projectId);
      foreignBoardIdsByProjectId.set(key, [
        ...(foreignBoardIdsByProjectId.get(key) || []),
        board.id,
      ]);
    }
    try {
      // eslint-disable-next-line no-await-in-loop
      const existing = await BoardMembership.qm.getOneByBoardIdAndUserId(board.id, user.id);
      if (visible) {
        if (!existing) {
          // eslint-disable-next-line no-await-in-loop
          await BoardMembership.qm.createOne({
            projectId: board.projectId,
            boardId: board.id,
            userId: user.id,
            role: BoardMembership.Roles.EDITOR,
          });
          result.created += 1;
        } else if (!legacy && existing.role !== BoardMembership.Roles.EDITOR) {
          // eslint-disable-next-line no-await-in-loop
          await BoardMembership.qm.updateOne(existing.id, { role: BoardMembership.Roles.EDITOR });
          result.promoted += 1;
        }
      } else if (existing) {
        // Mesma limpeza de helpers/board-memberships/delete-one (antes do delete:
        // se falhar, a membership fica e o próximo ciclo tenta de novo).
        // eslint-disable-next-line no-await-in-loop
        await revokeBoardAccessSideEffects({ boardId: board.id, userId: user.id, models });
        // eslint-disable-next-line no-await-in-loop
        await BoardMembership.qm.deleteOne(existing.id);
        leaveBoardRoom(board.id);
        result.revoked += 1;
      }
    } catch (error) {
      result.errors.push(`board(${board.id}): ${errorMessage(error)}`);
    }
  }

  // 2) Gerências antigas (só no modo isolado por grupo e nunca do admin interno).
  if (legacy || isInternalAdminUser(user, { internalAdminEmail, internalUserId })) {
    return result;
  }

  let projectManagers;
  try {
    projectManagers = (await ProjectManager.qm.getByUserId(user.id)) || [];
  } catch (error) {
    result.errors.push(`project-managers: ${errorMessage(error)}`);
    return result;
  }

  // eslint-disable-next-line no-restricted-syntax
  for (const projectManager of projectManagers) {
    const projectId = String(projectManager.projectId);
    const project = projectById.get(projectId);
    const foreignBoardIds = foreignBoardIdsByProjectId.get(projectId);
    // Fora de getShared() = projeto privado (ou inexistente): não mexe.
    if (!project || project.ownerProjectManagerId || !foreignBoardIds) continue; // eslint-disable-line no-continue
    try {
      // Gerente via inscrição sem membership: limpa inscrições/cards/tasks de
      // todo board do projeto que deixou de ser visível.
      // eslint-disable-next-line no-restricted-syntax
      for (const boardId of foreignBoardIds) {
        // eslint-disable-next-line no-await-in-loop
        await revokeBoardAccessSideEffects({ boardId, userId: user.id, models });
      }
      // eslint-disable-next-line no-await-in-loop
      await ProjectManager.qm.deleteOne(projectManager.id);
      foreignBoardIds.forEach(leaveBoardRoom);
      result.removedProjectManagers += 1;
    } catch (error) {
      result.errors.push(`project-manager(${projectManager.id}): ${errorMessage(error)}`);
    }
  }

  return result;
};

module.exports = {
  LEGACY_SHARED_GROUP,
  ensureSharedAccess,
  getMappedProjectIds,
  isInternalAdminUser,
};
