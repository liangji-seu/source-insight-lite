const fs = require('fs');
const path = require('path');

const extensionDir = path.resolve(__dirname, '..');
const source = path.resolve(extensionDir, '..', 'backend');
const destination = path.join(extensionDir, 'backend');

if (!fs.existsSync(source)) throw new Error(`Backend directory not found: ${source}`);
fs.rmSync(destination, { recursive: true, force: true });
fs.cpSync(source, destination, { recursive: true, filter: (name) => !name.includes('__pycache__') && !name.endsWith('.pyc') && !name.includes(`${path.sep}tests${path.sep}`) });

const cytoscape = path.join(extensionDir, 'node_modules', 'cytoscape', 'dist', 'cytoscape.min.js');
const media = path.join(extensionDir, 'media');
fs.mkdirSync(media, { recursive: true });
if (fs.existsSync(cytoscape)) fs.copyFileSync(cytoscape, path.join(media, 'cytoscape.min.js'));
else console.warn('cytoscape is not installed; run npm install before packaging');
