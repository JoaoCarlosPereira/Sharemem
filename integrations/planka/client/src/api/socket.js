/*!
 * Copyright (c) 2024 PLANKA Software GmbH
 * Licensed under the Fair Use License: https://github.com/plankanban/planka/blob/master/LICENSE.md
 */

import socketIOClient from 'socket.io-client';
import sailsIOClient from 'sails.io.js';

import Config from '../constants/Config';

const io = sailsIOClient(socketIOClient);

// Same-origin always: UI proxies /planka → sidecar (incl. Socket.IO polling).
// Do NOT retarget :8765/:8443 when the embed is on :3000 — that breaks CORS
// (browser origin :3000 → Traefik :8765) and bypasses the Next /planka proxy.
//
// Path SEM barra final: Next (trailingSlash=false) responde 308 em
// `/planka/socket.io/` → `/planka/socket.io`, o que quebra long-polling POSTs
// do Engine.IO de forma intermitente. O Route Handler já normaliza para
// `/socket.io/` no upstream Planka.
io.sails.url = window.location.origin;
io.sails.path = `${Config.BASE_PATH}/socket.io`;
io.sails.query = 'nosession=1';
io.sails.transports = ['polling'];
io.sails.autoConnect = false;
io.sails.reconnection = true;
io.sails.useCORSRouteToGetCookie = false;
io.sails.environment = import.meta.env.MODE;

const { socket } = io;

socket.connect = socket._connect; // eslint-disable-line no-underscore-dangle

['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].forEach((method) => {
  socket[method.toLowerCase()] = (url, data, headers) =>
    new Promise((resolve, reject) => {
      socket.request(
        {
          method,
          data,
          headers,
          url: `/api${url}`,
        },
        (body, { error }) => {
          if (body instanceof Error || error) {
            reject(body);
          } else {
            resolve(body);
          }
        },
      );
    });
});

export default socket;
