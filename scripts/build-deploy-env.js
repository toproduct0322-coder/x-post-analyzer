#!/usr/bin/env node
// .env + x_cookies.json → .deploy-env.yaml (Cloud Run --env-vars-file 用)
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

// .env パース (1行=1キー、末尾コメント切り捨て)
const envText = fs.readFileSync(path.join(ROOT, '.env'), 'utf8');
const env = {};
for (const line of envText.split(/\r?\n/)) {
  const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.*)$/);
  if (!m) continue;
  let v = m[2];
  if (v.length >= 2 && ((v[0] === '"' && v.endsWith('"')) || (v[0] === "'" && v.endsWith("'")))) {
    v = v.slice(1, -1);
  } else {
    v = v.split(/\s/)[0];
  }
  env[m[1]] = v;
}

// x_cookies.json
const cookies = JSON.parse(fs.readFileSync(path.join(ROOT, 'x_cookies.json'), 'utf8'));

const out = {
  ANTHROPIC_API_KEY: env.ANTHROPIC_API_KEY,
  CLOUDINARY_API_KEY: env.CLOUDINARY_API_KEY,
  CLOUDINARY_API_SECRET: env.CLOUDINARY_API_SECRET,
  CLOUDINARY_CLOUD_NAME: env.CLOUDINARY_CLOUD_NAME,
  PEXELS_API_KEY: env.PEXELS_API_KEY,
  X_AUTH_TOKEN: cookies.auth_token || '',
  X_CT0: cookies.ct0 || '',
  X_USERNAME: cookies.username || '',
};

const lines = [];
for (const [k, v] of Object.entries(out)) {
  if (v === undefined || v === null || v === '') continue;
  const s = String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');
  lines.push(`${k}: "${s}"`);
}
fs.writeFileSync(path.join(ROOT, '.deploy-env.yaml'), lines.join('\n') + '\n', { mode: 0o600 });
console.error('Wrote .deploy-env.yaml (' + lines.length + ' vars)');
