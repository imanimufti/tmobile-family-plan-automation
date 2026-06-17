#!/usr/bin/env node
/*
 * Headless WhatsApp Web sender for the T-Mobile bill pipeline.
 *
 * Drives a logged-in WhatsApp Web session (seeded once by scanning a QR code),
 * so the breakdown can be posted to the family group unattended — even while
 * the Mac is locked, which the WhatsApp Desktop keystroke approach can't do.
 *
 * Session persists under state/whatsapp_web/ (gitignored).
 *
 * Usage:
 *   node send.js seed                         # one-time: scan the QR with your phone
 *   node send.js list-groups                  # print group names + ids
 *   node send.js send --group "<name>" [--image <path>] --message-file <path>
 *   node send.js send --group-id "<id@g.us>" --message "<text>"
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { exec } = require('child_process');
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const QRImage = require('qrcode');

const DATA_PATH = path.join(__dirname, '..', 'state', 'whatsapp_web');

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const key = argv[i].slice(2);
      const val = (i + 1 < argv.length && !argv[i + 1].startsWith('--')) ? argv[++i] : true;
      args[key] = val;
    }
  }
  return args;
}

function makeClient() {
  return new Client({
    authStrategy: new LocalAuth({ clientId: 'tmobile', dataPath: DATA_PATH }),
    puppeteer: {
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    },
  });
}

function fail(msg, code = 1) {
  console.error('✗ ' + msg);
  process.exit(code);
}

async function resolveChat(client, args) {
  const chats = await client.getChats();
  const groups = chats.filter((c) => c.isGroup);
  if (args['group-id']) {
    const c = groups.find((g) => g.id._serialized === args['group-id']);
    if (c) return c;
    fail(`No group with id ${args['group-id']}`);
  }
  if (args.group) {
    const want = String(args.group).trim().toLowerCase();
    const c = groups.find((g) => (g.name || '').trim().toLowerCase() === want);
    if (c) return c;
    fail(`Group "${args.group}" not found. Available groups:\n  ` +
      groups.map((g) => `${g.name}  [${g.id._serialized}]`).join('\n  '));
  }
  fail('Specify --group "<name>" or --group-id "<id>"');
}

function getMessageText(args) {
  if (args['message-file']) return fs.readFileSync(args['message-file'], 'utf8');
  if (args.message) return String(args.message);
  return '';
}

async function main() {
  const cmd = process.argv[2];
  const args = parseArgs(process.argv.slice(3));
  const client = makeClient();

  // Hard timeout so a stuck session never hangs the pipeline.
  const guard = setTimeout(() => fail('Timed out waiting for WhatsApp Web', 2), 240000);

  client.on('loading_screen', (percent, message) =>
    console.log(`… loading WhatsApp Web ${percent}% ${message || ''}`));
  client.on('authenticated', () => console.log('… authenticated, syncing'));

  let qrOpened = false;
  const qrImgPath = path.join(os.tmpdir(), 'tmobile-whatsapp-qr.png');
  client.on('qr', (qr) => {
    if (cmd === 'seed') {
      console.log('\nScan in WhatsApp → Settings → Linked Devices → Link a Device.');
      qrcode.generate(qr, { small: true });  // terminal fallback
      // Also write a crisp PNG and open it full-size (far easier to scan).
      QRImage.toFile(qrImgPath, qr, { width: 500, margin: 3 }, (err) => {
        if (err) return;
        if (!qrOpened) {
          qrOpened = true;
          exec(`open "${qrImgPath}"`);
          console.log(`\n→ A scannable QR image opened on screen: ${qrImgPath}`);
          console.log('  (If it expires before you scan, just re-run `node send.js seed`.)');
        }
      });
    } else {
      clearTimeout(guard);
      fail('Not logged in — run `node send.js seed` and scan the QR first', 3);
    }
  });

  client.on('auth_failure', (m) => { clearTimeout(guard); fail('Auth failure: ' + m, 3); });

  client.on('ready', async () => {
    try {
      if (cmd === 'seed') {
        console.log('✓ WhatsApp Web session is ready and saved. You can close this.');
      } else if (cmd === 'list-groups') {
        const chats = await client.getChats();
        chats.filter((c) => c.isGroup).forEach((g) =>
          console.log(`${g.name}  [${g.id._serialized}]`));
      } else if (cmd === 'send') {
        const chat = await resolveChat(client, args);
        const text = getMessageText(args);
        if (args.image) {
          const media = MessageMedia.fromFilePath(args.image);
          await chat.sendMessage(media, { caption: text });
          console.log(`✓ Sent image + caption to "${chat.name}"`);
        } else {
          if (!text) fail('Nothing to send (no --message/--message-file and no --image)');
          await chat.sendMessage(text);
          console.log(`✓ Sent message to "${chat.name}"`);
        }
      } else {
        fail(`Unknown command "${cmd}" (use seed | list-groups | send)`);
      }
      clearTimeout(guard);
      // Give WhatsApp a moment to flush the outgoing message before teardown.
      setTimeout(async () => { await client.destroy(); process.exit(0); }, 2500);
    } catch (e) {
      clearTimeout(guard);
      fail(e && e.message ? e.message : String(e));
    }
  });

  client.initialize();
}

main();
