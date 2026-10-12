// Binary SSH transfer of the stopped recorder's local state; archive stays put.
import {spawn, execFileSync} from 'node:child_process';
import {pipeline} from 'node:stream/promises';

const ssh = process.env.FRIGATE_SSH_EXE || 'ssh';
const state = execFileSync(ssh, ['-o', 'BatchMode=yes', 'adler-frigate', 'sudo',
  'docker', 'inspect', 'frigate', '--format', '{{.State.Running}}'], {encoding: 'utf8'}).trim();
if (state !== 'false') throw new Error('Stop the White writer before transferring SQLite');
const blackNames = execFileSync(ssh, ['-o', 'BatchMode=yes', 'adler-black-u2.lan', 'sudo',
  'docker', 'ps', '--format', '{{.Names}}'], {encoding: 'utf8'}).trim().split('\n');
if (blackNames.includes('frigate')) throw new Error('Stop the Black writer before replacing SQLite');
const source = spawn(ssh, ['-o', 'BatchMode=yes', 'adler-frigate', 'sudo', 'tar',
  '-C', '/opt/frigate', '-cf', '-', 'config', 'certs', '.env'], {stdio: ['ignore', 'pipe', 'inherit']});
const target = spawn(ssh, ['-o', 'BatchMode=yes', 'adler-black-u2.lan', 'sudo', 'tar',
  '-C', '/opt/frigate', '-xf', '-'], {stdio: ['pipe', 'inherit', 'inherit']});
const exits = Promise.all([source, target].map(child => new Promise((resolve, reject) => {
  child.on('error', reject);
  child.on('exit', code => resolve(code));
})));
try {
  await pipeline(source.stdout, target.stdin);
  const codes = await exits;
  if (codes.some(code => code !== 0)) throw new Error('SSH transfer failed: ' + codes.join(','));
  console.log('Closed SQLite, configuration and certificates transferred');
} catch (error) {
  source.kill(); target.kill();
  console.error(error.message);
  process.exitCode = 1;
}
