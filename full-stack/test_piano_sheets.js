/* 面板开关的静态断言。`node test_piano_sheets.js`

   .sheet 是靠 **show** 这个类滑上来的（`.sheet.show` 收起、
   `.sheet.show.expanded` 全屏），不是 `open`。

   08-12 踩过：歌曲详情 / 听歌档案 / 房间设置三个面板都写成了
   classList.add('open')，CSS 不认这个类，于是按钮明明响应了、面板却
   一直待在屏幕外——在她那儿的表现就是「点了没反应」。
   这种错不报任何异常，只能靠断言拦。 */
const fs = require('fs');
const path = require('path');

const root = __dirname;
const html = fs.readFileSync(
  process.argv[2] || path.join(root, 'static', 'index.html'), 'utf8');
const css = fs.readFileSync(
  process.argv[3] || path.join(root, 'static', 'design-system.css'), 'utf8');

const bad = [];
/* 只认真正带 class="sheet" 的那些。皮肤面板 (#pianoSkinSheet) 走的是
   遮罩的 hidden，不是 .sheet 那一套，别把它算进来。 */
const sheets = [...html.matchAll(/<section[^>]*class="[^"]*\bsheet\b[^"]*"[^>]*id="(\w+)"/g)]
  .map(m => m[1])
  /* 只管琴房自己那几个。Chat 的 thoughtSheet / modelSheet 走的是全局
     sheet(name,open) 那套（靠 xxxSheet / xxxOverlay 的命名约定），不归这儿管。 */
  .filter(id => id.startsWith('piano'));

// 1) 这份断言的前提：CSS 里确实是 .sheet.show 生效
if (!/\.sheet\.show/.test(css)) {
  bad.push('CSS 里找不到 .sheet.show —— 这份断言的前提变了，先看 design-system.css');
}

// 2) 谁都不许再用 open 类开关面板
//    🔴 必须**跨行**匹配：原 bug 长这样——取元素和加类分在两行，
//    const sh=$('pianoSetSheet');\n  ...\n  sh.classList.add('open');
//    用 [^\n] 的话正好漏掉的就是它，等于白写这条断言。
sheets.forEach(id => {
  const re = new RegExp("\\$\\('" + id + "'\\)[\\s\\S]{0,240}?classList\\.(add|toggle)\\('open'");
  if (re.test(html)) bad.push(id + ' 用 open 类开关（CSS 不认，面板不会出现）');
});

// 3) 每个面板都得有人能关掉它——不然就是上次那个「打开了关不掉」
sheets.forEach(id => {
  const closable =
    new RegExp("pianoSheet\\(\\s*\\$\\('" + id + "'\\)\\s*,\\s*false\\)").test(html) ||
    (new RegExp("const sh\\s*=\\s*\\$\\('" + id + "'\\)").test(html) &&
     /pianoSheet\(sh,\s*false\)/.test(html)) ||
    (id === 'pianoDrawer' && /function pianoDrawer\(open\)/.test(html));
  if (!closable) bad.push(id + ' 找不到关闭路径');
});

// 4) 遮罩关闭态必须是 display:none，不许留透明层压 z-index（AGENTS.md）
if (!/\.piano-mask\[hidden\]\s*\{\s*display:\s*none/.test(css)) {
  bad.push('.piano-mask[hidden] 没有 display:none —— 关着的时候会挡住点击');
}

if (bad.length) {
  console.log('FAILED:');
  bad.forEach(b => console.log('  -', b));
  process.exit(1);
}
console.log('面板开关断言：全部通过（共 ' + sheets.length + ' 个面板）');
