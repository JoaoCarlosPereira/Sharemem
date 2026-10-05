/*!
 * Copyright (c) 2024 PLANKA Software GmbH
 * Licensed under the Fair Use License: https://github.com/plankanban/planka/blob/master/LICENSE.md
 */

const { isMem0BridgeActive } = require('../../../utils/mem0-group-scope');

module.exports = {
  sync: true,

  inputs: {
    internalConfig: {
      type: 'ref',
      required: true,
    },
    oidc: {
      type: 'ref',
    },
    user: {
      type: 'ref',
    },
  },

  fn(inputs) {
    const data = {
      oidc: inputs.oidc,
      termsLanguages: sails.hooks.terms.getLanguages(),
      version: sails.config.custom.version,
    };

    if (inputs.user && inputs.user.role === User.Roles.ADMIN) {
      Object.assign(data, {
        activeUsersLimit: inputs.internalConfig.activeUsersLimit,
        customerPanelUrl: sails.config.custom.customerPanelUrl,
      });
    }

    // Mem0 Shared: com a ponte de auth ativa, o servidor recorta projetos/boards
    // pelo grupo do usuário; o client agrupa tudo em "Equipe" (sem "Outros").
    if (isMem0BridgeActive()) {
      data.isMem0Shared = true;
    }

    if (sails.config.custom.demoMode) {
      data.isDemoMode = true;
    }

    return data;
  },
};
