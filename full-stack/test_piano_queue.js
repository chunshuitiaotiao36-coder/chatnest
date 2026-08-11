/* 队列推进的断言。`node test_piano_queue.js`，不用浏览器、不用部署。

   守的是 queue / browse 分家那次改名（41 处）和播放模式的推进逻辑：
   queue 是正在放的那一串，browse 是抽屉里正在看的那一串，两套下标。
   合回一个的话「加入待播」就成了假按钮，列表高亮也会串行。
   做法是把 index.html 里那几个纯函数抠出来，喂桩跑。 */
const fs = require('fs');
const path = require('path');
const target = process.argv[2] || path.join(__dirname, 'static', 'index.html');
const src = fs.readFileSync(target, 'utf8').replace(/\r/g, '');

function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('找不到 ' + name);
  let d = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (!d) return src.slice(i, k + 1); }
  }
}

// 依赖的桩
let played = [];
const piano = {
  queue: [], browse: [], idx: -1, mode: 'loop', plan: [], prefetch: {},
  kind: '', browseKind: '', sel: null, view: 'songs', listName: '',
};
const toast = () => {};
const pianoPlay = i => { piano.idx = i; played.push(i); };
const pianoFmMore = () => { played.push('fm-more'); };
const pianoPlanAhead = () => {};
const api = () => Promise.resolve({ ok: false });
const $ = () => null;
const localStorage = { getItem: () => null, setItem: () => {} };
const PIANO_MODES = [{ id: 'loop' }, { id: 'one' }, { id: 'shuffle' }];

eval(grab('pianoPlanPush'));
eval(grab('pianoUpcoming'));
eval(grab('pianoNext'));
eval(grab('pianoEnded'));
eval(grab('pianoPlayBrowse'));
eval(grab('pianoQueueAppend'));
eval(grab('pianoReplaceQueue'));

const fails = [];
const eq = (n, got, want) => {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) { fails.push(`${n}\n     得到 ${a}\n     期望 ${b}`); console.log('  FAIL  ' + n); }
  else console.log('  ok    ' + n);
};
const songs = n => Array.from({ length: n }, (_, i) => ({ id: String(100 + i), title: 's' + i }));
const reset = (q, i, mode, kind) => {
  piano.queue = songs(q); piano.idx = i; piano.mode = mode || 'loop';
  piano.kind = kind || ''; piano.plan = []; piano.prefetch = {}; played = [];
};

// ── queue / browse 分家 ──
piano.browse = songs(5); piano.browseKind = 'fm'; piano.queue = []; piano.idx = -1;
pianoPlayBrowse(2);
eq('点浏览列表第 3 首：整串接管成队列', piano.queue.length, 5);
eq('点浏览列表第 3 首：从第 3 首开始放', piano.idx, 2);
eq('接管时把 kind 带过去', piano.kind, 'fm');

piano.browseKind = '';
reset(3, 0);
piano.browse = songs(3);
pianoQueueAppend({ id: 'x', title: 'x' });
eq('加入待播：只动 queue', piano.queue.length, 4);
eq('加入待播：不动 browse', piano.browse.length, 3);

reset(3, 1);
pianoReplaceQueue(songs(2), 0);
eq('换队列会清掉随机计划', piano.plan, []);
eq('换队列会清掉预取表', Object.keys(piano.prefetch), []);

// ── 列表循环 ──
reset(3, 2, 'loop');
pianoNext(1);
eq('列表循环：最后一首的下一首绕回第一首', played, [0]);
reset(3, 0, 'loop');
pianoNext(-1);
eq('列表循环：第一首的上一首绕到最后', played, [2]);

// ── 单曲循环 ──
reset(3, 1, 'one');
pianoNext(1);
eq('单曲循环：手动点下一首仍然走下一首', played, [2]);

// ── 随机 ──
reset(6, 0, 'shuffle');
pianoNext(1);
eq('随机：跳到计划里的一首', played.length, 1);
eq('随机：不会原地不动', played[0] !== 0, true);

reset(6, 0, 'shuffle');
const up = pianoUpcoming(3);
eq('随机：一次抽满三步', up.length, 3);
eq('随机：三步互不重复', new Set(up).size, 3);
eq('随机：三步都不是当前这首', up.includes(0), false);
eq('随机：计划就是这三步（只有一份计划）', piano.plan.slice(0, 3), up);

// ── FM 放到底是续，不是绕回 ──
reset(3, 2, 'loop', 'fm');
pianoNext(1);
eq('FM：放到底去请求新歌', played, ['fm-more']);
reset(3, 2, 'loop', 'fm');
eq('FM：预取不绕回开头', pianoUpcoming(3), []);

// ── 单曲循环自然放完 ──
reset(3, 1, 'one');
let rewound = false;
global.pianoAudio = { currentTime: 30, play: () => { rewound = true; return Promise.resolve(); } };
pianoEnded();
eq('单曲循环：自然放完是重头再放，不切歌', played, []);
eq('单曲循环：进度归零', global.pianoAudio.currentTime, 0);

// ── 预取窗口 ──
reset(5, 0, 'loop');
eq('列表循环：预取接下来三首', pianoUpcoming(3), [1, 2, 3]);
reset(5, 3, 'loop');
eq('列表循环：跨过队尾会绕回去', pianoUpcoming(3), [4, 0, 1]);
reset(5, 0, 'one');
eq('单曲循环：不预取', pianoUpcoming(3), []);

console.log();
if (fails.length) { console.log('FAILED:'); fails.forEach(f => console.log(' -', f)); process.exit(1); }
console.log('全部通过');
