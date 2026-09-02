/* 底部导航栏「焊死」的回归测试。
 *
 * 这个 bug 从 8-19 到 9-02 反复了十几轮，每一轮都是「云端量着是好的、真机上是坏的」。
 * 09-02 她的两张真机截图逐像素量出来：
 *     导航栏 css 703..793（90px = --tabbar-h 56 + safe-area-bottom 34，高度是对的）
 *     屏幕 852，底下空着 59px，壁纸层 #bgLayer 也停在 793
 * ⇒ 布局视口就是 793 高，fixed;bottom:0 只能贴到布局视口的底，贴不到屏幕底。
 *
 * 云端 Chromium 里 innerHeight === documentElement.clientHeight，这个差**永远造不出来**，
 * 所以下面第 2 段用 defineProperty 把 innerHeight 抬高 59 来复现她的机器——
 * 差值一样，weldTabbar() 看到的 gap 就一样。这是这个 bug 唯一能在云端跑的判据。
 *
 * 怎么跑（先起 harness，见会话记录）：
 *     node test_navbar.mjs
 *
 * 🔴 路径和 token 都写死成本地开发用的，跑之前照自己的环境改。
 */
import { chromium } from '/opt/node22/lib/node_modules/playwright/index.mjs';
const T='a8b92330aee6b8000251e2a7c4566157f6cc565741355606612e7cf472302370';
const b=await chromium.launch({executablePath:'/opt/pw-browsers/chromium-1194/chrome-linux/chrome'});
let pass=0,fail=0;const ok=(c,m)=>{c?pass++:fail++;console.log((c?'  PASS  ':'* FAIL *')+'  '+m)};

const ctx=await b.newContext({viewport:{width:393,height:852},deviceScaleFactor:2,
  isMobile:true,hasTouch:true,colorScheme:'light'});
await ctx.addInitScript(t=>localStorage.setItem('chat_token',t),T);
await ctx.route(u=>!String(u).includes('127.0.0.1'),r=>r.abort());
const p=await ctx.newPage();
const errs=[];p.on('pageerror',e=>errs.push(String(e&&e.stack||e).slice(0,300)));
await p.route('**/api/**',r=>r.fulfill({status:200,contentType:'application/json',body:'{}'}));
await p.goto('http://127.0.0.1:8877/',{waitUntil:'domcontentloaded'});
await p.waitForTimeout(800);
await p.evaluate(()=>{const s=document.querySelector('.mobile-sidebar');if(s)s.style.display='none'});

const d=await p.evaluate(()=>{
  const bar=document.getElementById('tabBar');
  const out={};
  const R=()=>Math.round(bar.getBoundingClientRect().bottom);
  const inline=()=>bar.style.bottom||'(未设)';

  // ── 1. 正常机器（innerHeight == clientHeight）：焊死不该动手 ──
  weldTabbar();
  out.normal={bottom:R(), inline:inline(), ih:innerHeight, ch:document.documentElement.clientHeight};

  // ── 2. 复现她的机器：布局视口比屏幕矮 59px ──
  //    真机上 clientHeight=793 / innerHeight=852。这里没法把 clientHeight 改小，
  //    就把 innerHeight 抬高 59——差值一样，焊死看到的 gap 一样。
  const REAL = window.innerHeight;
  Object.defineProperty(window,'innerHeight',{configurable:true,get:()=>REAL+59});
  weldTabbar();
  out.hers={bottom:R(), inline:inline(), gapAfter:Math.round(window.innerHeight-bar.getBoundingClientRect().bottom)};

  // 幂等：再调一次不该继续往下推
  weldTabbar(); weldTabbar();
  out.idem={bottom:R(), inline:inline()};

  // ── 3. 键盘弹起时不许跟它抢 ──
  bar.style.removeProperty('bottom');
  document.body.classList.add('keyboard-open');
  weldTabbar();
  out.kb={inline:inline()};
  document.body.classList.remove('keyboard-open');

  // ── 4. 进对话（display:none）不许动 ──
  document.body.classList.add('in-chat');
  weldTabbar();
  out.inchat={inline:inline()};
  document.body.classList.remove('in-chat');

  // ── 5. 回到正常机器：焊过的 inline 要自己撤掉 ──
  weldTabbar();                       // 先在「她的机器」下焊上
  out.beforeRestore=inline();
  // 🔴 不能 delete —— window.innerHeight 被 defineProperty 之后 delete 掉
  //    会让它变成 undefined，weldTabbar 的 `if(!vh) return` 就直接早退，
  //    量到的不是「撤掉了没有」而是「根本没跑」。要把真值定义回去。
  Object.defineProperty(window,'innerHeight',{configurable:true,get:()=>REAL});
  weldTabbar();
  out.restored={bottom:R(), inline:inline(), ih:window.innerHeight};

  // ── 6. 焊死块只管定位，没把 transform / z-index 掀翻 ──
  const cs=getComputedStyle(bar);
  out.css={position:cs.position, zIndex:cs.zIndex,
           transformOK:cs.transform==='none'||/matrix/.test(cs.transform)};
  // 焊死块有没有声明 transform（当年那条 transform:none!important 的教训）
  out.inlineTransform=bar.style.transform||'(未设)';
  out.declaresTransform=(()=>{for(const sh of document.styleSheets){try{
      for(const r of sh.cssRules){
        if(r.selectorText==='#tabBar'&&r.style&&r.style.getPropertyPriority('transform')==='important')return true;
      }}catch(e){}}return false})();
  return out;
});
console.log(JSON.stringify(d,null,1));

ok(d.normal.bottom===852&&d.normal.inline==='(未设)',
   `正常机器不动手（bottom=${d.normal.bottom}，inline=${d.normal.inline}）`);
ok(d.hers.inline==='-59px', `她那种视口下焊出 -59px（实到 ${d.hers.inline}）`);
ok(d.hers.gapAfter===0, `焊完之后离屏幕底 0px（实到 ${d.hers.gapAfter}）`);
ok(d.idem.inline==='-59px', `再调两次不继续推（实到 ${d.idem.inline}）`);
ok(d.kb.inline==='(未设)', `键盘弹起时不抢（inline=${d.kb.inline}）`);
ok(d.inchat.inline==='(未设)', `进对话隐藏时不动（inline=${d.inchat.inline}）`);
ok(d.restored.inline==='(未设)'&&d.restored.bottom===852,
   `换回正常视口能自己撤掉（inline=${d.restored.inline}，bottom=${d.restored.bottom}）`);
ok(d.css.position==='fixed', `position 还是 fixed`);
ok(d.css.zIndex==='25', `z-index 仍是刻意设的 25，没被焊死块改成 81（实到 ${d.css.zIndex}）`);
ok(d.inlineTransform==='(未设)'&&d.declaresTransform===false,
   `焊死块一个字都没碰 transform（inline=${d.inlineTransform}，有 !important 规则=${d.declaresTransform}）`);
ok(errs.filter(e=>!/loadModels/.test(e)).length===0, `无 JS 报错${errs.length?'：'+errs[0]:''}`);

console.log(`\n合计 ${pass} 通过 / ${fail} 失败`);
await ctx.close(); await b.close(); process.exit(fail?1:0);
