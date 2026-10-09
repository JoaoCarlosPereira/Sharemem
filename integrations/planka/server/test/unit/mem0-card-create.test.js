/*!
 * Unit tests for the Mem0 card-created bridge rules (no Sails lift).
 *   node --test test/unit/mem0-card-create.test.js
 */

const assert = require('assert');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { describe, it } = require('node:test');

const {
  shouldNotifyCardCreated,
  buildCardCreatedBody,
} = require('../../utils/mem0-card-create');

describe('mem0 card-created bridge', () => {
  it('notifies Spec only for human sessions (authMethod === jwt)', () => {
    assert.strictEqual(shouldNotifyCardCreated({ authMethod: 'jwt' }), true);
  });

  it('never notifies for any other auth method (whitelist)', () => {
    ['internal', 'omtk_', 'legacy', 'disabled', 'public', undefined, null, '', 'JWT'].forEach(
      (authMethod) => {
        assert.strictEqual(shouldNotifyCardCreated({ authMethod }), false, String(authMethod));
      },
    );
  });

  it('builds the snake_case payload expected by /api/v1/specs/planka/card-created', () => {
    assert.deepStrictEqual(
      buildCardCreatedBody({
        plankaCardId: 123,
        plankaListId: 456,
        name: 'Card',
        description: undefined,
        dueDate: null,
        position: 65536,
        actor: '',
      }),
      {
        planka_card_id: '123',
        planka_list_id: '456',
        name: 'Card',
        description: null,
        due_date: null,
        position: 65536,
        actor: 'ui-user',
      },
    );
  });

  it('cards/create controller gates the bridge on authMethod only', () => {
    const source = fs.readFileSync(
      path.join(__dirname, '../../api/controllers/cards/create.js'),
      'utf8',
    );
    assert.match(source, /shouldNotifyCardCreated\(\{ authMethod \}\)/);
    assert.match(source, /notifySpecCardCreate\.with\(/);
    assert.doesNotMatch(source, /X-Mem0-Mirror/);
  });

  it('helper logs a warning when the bridge fails (card kept)', async () => {
    const server = http.createServer((req, res) => {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end('{"detail":"boom"}');
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const warnings = [];
    const previous = {
      sails: global.sails,
      url: process.env.OPENMEMORY_INTERNAL_URL,
      token: process.env.OPENMEMORY_BRIDGE_TOKEN,
    };
    global.sails = { log: { warn: (...args) => warnings.push(args) } };
    process.env.OPENMEMORY_INTERNAL_URL = `http://127.0.0.1:${server.address().port}`;
    process.env.OPENMEMORY_BRIDGE_TOKEN = 'test-token';
    try {
      const helper = require('../../api/helpers/mem0/notify-spec-card-create');
      await new Promise((resolve) => {
        helper.fn({ plankaCardId: '42', plankaListId: '7', name: 'X' }, { success: resolve });
      });
    } finally {
      server.close();
      global.sails = previous.sails;
      process.env.OPENMEMORY_INTERNAL_URL = previous.url;
      process.env.OPENMEMORY_BRIDGE_TOKEN = previous.token;
      if (previous.url === undefined) delete process.env.OPENMEMORY_INTERNAL_URL;
      if (previous.token === undefined) delete process.env.OPENMEMORY_BRIDGE_TOKEN;
    }
    assert.strictEqual(warnings.length, 1);
    assert.match(warnings[0][0], /card 42 NOT imported/);
    assert.strictEqual(warnings[0][1], 500);
  });
});
