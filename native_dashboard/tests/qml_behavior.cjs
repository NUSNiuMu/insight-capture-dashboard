// Execute the production QML JavaScript handlers with ordinary fake players.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
function block(source, marker) {
    const start = source.indexOf(marker);
    assert(start >= 0, marker);
    const brace = source.indexOf('{', start);
    let depth = 1, quote = '';
    for (let i=brace+1; i<source.length; ++i) {
        const c = source[i];
        if (quote) {
            if (c === '\\') ++i;
            else if (c === quote) quote = '';
        } else if (c === '"' || c === "'") quote = c;
        else if (c === '{') ++depth;
        else if (c === '}' && --depth === 0) return source.slice(brace+1, i);
    }
    throw Error('Unclosed handler: ' + marker);
}
const main = fs.readFileSync('native_dashboard/Main.qml', 'utf8');
const wristPreviewOptions = new Function('dashboard', block(main, 'function wristPreviewOptions()'));
const cameraGroups = new Function('dashboard', 'wristPreviewCameras', 'selectedWristPreview',
    block(main, 'function cameraGroups()'));
const headCameras = ['side_left', 'stereo_left', 'stereo_right', 'side_right'].map(name => ({
    name, device: 'egohalo', row: 0, column: 1,
}));
const wristCameras = ['right', 'left'].map((side, row) => ({
    name: `uvc_${side}`, device: `uvc_${side}`, row, column: 0, uq2168: true,
}));
const sixCameras = [...wristCameras, ...headCameras];
const live = {playback: false, cameras: sixCameras};
const wristOptions = wristPreviewOptions(live);
assert.deepEqual(wristOptions.map(camera => camera.name), ['uvc_left', 'uvc_right']);
assert.deepEqual(cameraGroups(live, wristOptions, '').map(({name, row, column}) => [name, row, column]), [
    ['side_left', 0, 0], ['stereo_left', 0, 1], ['stereo_right', 1, 0], ['side_right', 1, 1],
]);
for (const wrist of wristOptions) {
    assert.deepEqual(cameraGroups(live, wristOptions, wrist.name).map(group => group.name), [wrist.name]);
}
const legacy = {playback: false, cameras: sixCameras.map(camera => ({...camera, uq2168: false}))};
assert.equal(cameraGroups(legacy, wristPreviewOptions(legacy), '').length, 3);
const playback = {...live, playback: true};
assert.equal(cameraGroups(playback, wristPreviewOptions(playback), '').length, 6);

const dashboard = {playback:true, cameras:[{name:'head'}, {name:'wrist'}], times:[],
    setPlaybackTime(value) {this.times.push(value)}};
const players = ['head', 'wrist'].map(name => ({cameraName:name, playbackReady:false, position:0,
    seeks:[], seek(value) {this.seeks.push(value)}}));
const root = {playbackReady:false, playTime:0, allVideos(fn) {players.forEach(fn)},
    playbackMaster() {return 'head'}};
const timeline = {pressed:false};
const tick = new Function('root', 'dashboard', 'timeline', block(main, 'onTriggered:'));
tick(root, dashboard, timeline);
assert.equal(root.playbackReady, false);
players[0].playbackReady = true;
tick(root, dashboard, timeline);
assert.equal(root.playbackReady, false); // Wait for all streams, regardless of startup order.
players[1].playbackReady = true;
tick(root, dashboard, timeline);
assert.equal(root.playbackReady, true);
players[0].position = 5; players[1].position = 3;
tick(root, dashboard, timeline);
assert.deepEqual(players.map(p => p.seeks), [[], [5]]);
const stats = new Function('root', 'dashboard', 'timeline', 'position', 'cameraName', block(main, 'onStatsChanged:'));
stats(root, dashboard, timeline, 3, 'wrist');
assert.deepEqual(dashboard.times, []);
stats(root, dashboard, timeline, 5, 'head');
assert.equal(root.playTime, 5);
assert.deepEqual(dashboard.times, [5]);

const optimization = fs.readFileSync('native_dashboard/pages/OptimizationPage.qml', 'utf8');
const page = {loadedRun:'run-a', requestedRun:'', failedRun:'', trajectory:{vio:[[1,2,3]]}, status:{}};
const workspace = {data:{}, request() {}};
const load = vm.runInNewContext('(function(name){' + block(optimization, 'function load(name)') + '})', page);
page.workspace = workspace;
const completed = new Function('page', 'workspace', 'key', 'success', block(optimization, 'function onCompleted(key, success)'));
load('run-b');
completed(page, workspace, 'trajectories', false);
assert.equal(page.loadedRun, 'run-a');
assert.deepEqual(page.trajectory.vio, [[1,2,3]]);
assert.equal(page.requestedRun, '');
load('run-b'); // A failed load is retryable.
workspace.data.trajectories = {run_name:'run-b', vio:[[4,5,6]]};
completed(page, workspace, 'trajectories', true);
assert.equal(page.loadedRun, 'run-b');
assert.equal(page.trajectory, workspace.data.trajectories);
console.log('QML playback preroll/master/follower and optimization retry regressions passed');
