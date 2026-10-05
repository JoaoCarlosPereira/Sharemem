/*!
 * Mem0 Shared — notify Spec SoT after a human creates a card in the PLANKA UI.
 *
 * Best-effort: a falha do bridge NÃO desfaz a criação do card. Ela gera um
 * `warn` no log do PLANKA; o card só é recuperado pelo backfill admin.
 */

const http = require('http');
const https = require('https');
const { URL } = require('url');

const { buildCardCreatedBody } = require('../../../utils/mem0-card-create');

module.exports = {
  friendlyName: 'Notify Spec card create',

  description: 'Imports a card created by a human in the PLANKA UI as a Spec task.',

  inputs: {
    plankaCardId: { type: 'string', required: true },
    plankaListId: { type: 'string', required: true },
    name: { type: 'string', allowNull: true },
    description: { type: 'string', allowNull: true },
    dueDate: { type: 'string', allowNull: true },
    position: { type: 'number', allowNull: true },
    actor: { type: 'string', allowNull: true },
  },

  exits: {
    success: { description: 'Bridge imported (or ignored) the card.' },
  },

  async fn(inputs, exits) {
    const base = String(process.env.OPENMEMORY_INTERNAL_URL || '').trim().replace(/\/$/, '');
    const token = String(
      process.env.OPENMEMORY_BRIDGE_TOKEN || process.env.INTERNAL_ACCESS_TOKEN || '',
    ).trim();

    if (!base || !token) {
      sails.log.warn('mem0 notify-spec-card-create: OPENMEMORY_INTERNAL_URL/token missing; skip');
      return exits.success({ skipped: true });
    }

    const body = JSON.stringify(buildCardCreatedBody(inputs));
    const url = new URL(`${base}/api/v1/specs/planka/card-created`);
    const transport = url.protocol === 'https:' ? https : http;
    const result = await new Promise((resolve) => {
      const req = transport.request(
        {
          protocol: url.protocol,
          hostname: url.hostname,
          port: url.port || (url.protocol === 'https:' ? 443 : 80),
          path: url.pathname,
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(body),
            Authorization: `Bearer ${token}`,
          },
          timeout: 3000,
        },
        (res) => {
          const chunks = [];
          res.on('data', (chunk) => chunks.push(chunk));
          res.on('end', () => {
            const text = Buffer.concat(chunks).toString('utf8');
            let parsed = null;
            try {
              parsed = text ? JSON.parse(text) : null;
            } catch (_err) {
              parsed = { raw: text };
            }
            resolve({ status: res.statusCode || 0, body: parsed });
          });
        },
      );
      req.on('error', (err) => resolve({ status: 0, body: { error: err.message } }));
      req.on('timeout', () => {
        req.destroy();
        resolve({ status: 0, body: { error: 'timeout' } });
      });
      req.write(body);
      req.end();
    });

    if (!(result.status >= 200 && result.status < 300)) {
      sails.log.warn(
        `mem0 notify-spec-card-create failed: card ${inputs.plankaCardId} NOT imported ` +
          'as Spec task (recover with admin backfill)',
        result.status,
        result.body,
      );
    }
    return exits.success(result.body || { status: result.status });
  },
};
