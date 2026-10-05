/*!
 * Mem0 Shared — regras puras do webhook card-created (testáveis sem Sails).
 */

/**
 * O card deve ser notificado ao Spec? Lista branca: só criações de pessoa
 * logada na UI (sessão JWT). `internal` (espelho Spec → PLANKA, que já grava o
 * vínculo), `omtk_`, `legacy`, `disabled`/`public` e ausência de auth nunca
 * notificam — notificar o espelho importaria o próprio card como task duplicada.
 */
const shouldNotifyCardCreated = ({ authMethod }) => authMethod === 'jwt';

const buildCardCreatedBody = (inputs) => ({
  planka_card_id: String(inputs.plankaCardId),
  planka_list_id: String(inputs.plankaListId),
  name: inputs.name == null ? null : inputs.name,
  description: inputs.description == null ? null : inputs.description,
  due_date: inputs.dueDate == null ? null : inputs.dueDate,
  position: inputs.position == null ? null : inputs.position,
  actor: inputs.actor || 'ui-user',
});

module.exports = { shouldNotifyCardCreated, buildCardCreatedBody };
