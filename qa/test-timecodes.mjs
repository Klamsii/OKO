import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
// Load this browser module without adding a package-wide Node module mode.
const source=await readFile(new URL('../web/player.js',import.meta.url),'utf8');
const {formatTime}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
assert.equal(formatTime(0.03,true),'00:00.030');
assert.equal(formatTime(4.667,true),'00:04.667');
assert.equal(formatTime(59.9999,true),'01:00.000');
assert.equal(formatTime(3600.1,true),'1:00:00.100');
assert.equal(formatTime(4.667,false),'00:04');
assert.equal(formatTime(NaN,true),'00:00');
console.log('6 timestamp checks passed');
