#!/usr/bin/env node
/**
 * Pair WhatsApp with an 8-character linking code instead of a QR scan.
 *
 * Writes the Baileys session to the same directory bridge.js reads, so the gateway
 * picks it up unchanged. On the phone: WhatsApp → Linked devices → Link a device →
 * "Link with phone number instead", then type the printed code.
 *
 * Usage:
 *   node pair_code.mjs <phone-with-country-code> [session-dir]
 *   node pair_code.mjs 5511999998888 ~/.hermes/whatsapp/session
 */

import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason, Browsers } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { mkdirSync } from 'fs';

const phone = String(process.argv[2] || '').replace(/\D/g, '');
const sessionDir = process.argv[3]
  || path.join(process.env.HERMES_HOME || path.join(process.env.HOME || '~', '.hermes'), 'whatsapp', 'session');

if (!phone) {
  console.error('Usage: node pair_code.mjs <phone-with-country-code> [session-dir]');
  process.exit(2);
}
mkdirSync(sessionDir, { recursive: true });

let codeRequested = false;

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  if (state.creds.registered) {
    console.log(`Already paired (${sessionDir}). Delete the directory to pair again.`);
    process.exit(0);
  }
  const { version } = await fetchLatestBaileysVersion();
  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    printQRInTerminal: false,
    // Linking codes are rejected for unrecognised browser descriptors.
    browser: Browsers.ubuntu('Chrome'),
    syncFullHistory: false,
    markOnlineOnConnect: false,
  });
  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    // The first `qr` event means the socket is ready to request a code instead.
    if (qr && !codeRequested) {
      codeRequested = true;
      const code = await sock.requestPairingCode(phone);
      console.log(`PAIRING_CODE ${code.slice(0, 4)}-${code.slice(4)}`);
      console.log('WhatsApp → Linked devices → Link a device → Link with phone number instead');
    }
    if (connection === 'open') {
      console.log(`PAIRED ${sock.user?.id || ''} → ${sessionDir}`);
      // Let the post-pairing key sync flush to disk before exiting.
      setTimeout(() => process.exit(0), 8000);
    }
    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      if (reason === DisconnectReason.loggedOut) {
        console.log('LOGGED_OUT — code rejected or expired; delete the session dir and retry.');
        process.exit(1);
      }
      // 515 (restart required) is the normal step right after the code is accepted.
      start();
    }
  });
}

start();
