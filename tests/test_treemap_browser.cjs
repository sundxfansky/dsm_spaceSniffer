const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const GiB=1024**3;
const folders = [
  ['/volume1',1000,0], ['/volume1/影视库',530,0], ['/volume1/影视库/电影',360,320],
  ['/volume1/影视库/电影/收藏',40,40], ['/volume1/影视库/剧集',170,150], ['/volume1/影视库/剧集/纪录片',20,20],
  ['/volume1/备份',250,0], ['/volume1/备份/MacBook',180,180], ['/volume1/备份/系统镜像',70,70],
  ['/volume1/照片',140,120], ['/volume1/照片/2025',20,20], ['/volume1/文档',80,80],
  ['/volume2',200,200],
];
const result={complete:false,scanned_files:14000,settings:{large_file_mb:1024},warnings:[],
  volumes:['/volume1','/volume2'].map(path=>({path,filesystem:'btrfs',total_bytes:2000*GiB,used_bytes:1000*GiB,available_bytes:1000*GiB,usage_percent:50})),
  directories:[],large_directories:[],large_directories_total:0,hardlinks:[],hardlink_groups_total:0,
  tree_directories:folders.map(([path,size,direct])=>({path,size_bytes:size*GiB,file_count:1000,direct_size_bytes:direct*GiB,direct_file_count:direct?100:0})),
  large_files:[{path:'/volume1/影视库/电影/Interstellar.mkv',volume:'/volume1',size_bytes:80*GiB,link_count:1},
    {path:'/volume1/备份/系统镜像/nas-backup.iso',volume:'/volume1',size_bytes:60*GiB,link_count:1}],large_files_total:2};
(async()=>{
  const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1100}});
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/api/discover',r=>r.fulfill({json:{host:'demo.invalid',server_fingerprint:'DEMO',identity:{uid:0,username:'demo'},volumes:result.volumes}}));
    await page.route('**/api/jobs',r=>r.fulfill({json:{job_id:'demo'}}));
    await page.route('**/api/jobs/demo',r=>r.fulfill({json:{status:'running',progress:30,stage:'演示扫描',result}}));
    const tinyFiles=Array.from({length:120},(_,i)=>({type:'file',path:`/volume1/文档/note${String(i).padStart(3,'0')}.txt`,size_bytes:i*1024,link_count:1,file_count:1}));
    await page.route('**/api/jobs/demo/tree?*', r=> {
      const params=new URL(r.request().url()).searchParams;
      const path=params.get('path'), q=params.get('q')||'', offset=Number(params.get('offset'));
      const dirs=result.tree_directories.filter(n=>n.path.slice(0,n.path.lastIndexOf('/'))===path).map(n=>({...n,type:'folder'}));
      const files=[...result.large_files,...(path==='/volume1/文档'?tinyFiles:[])].filter(n=>n.path.slice(0,n.path.lastIndexOf('/'))===path).map(n=>({...n,type:'file',file_count:1}));
      const all=[...dirs,...files].sort((a,b)=>b.size_bytes-a.size_bytes||a.path.localeCompare(b.path));
      const filtered=all.filter(n=>n.path.toLowerCase().includes(q.toLowerCase()));
      return r.fulfill({json:{path,items:filtered.slice(offset,offset+30),offset,total:filtered.length,limit:30,map_items:all.slice(0,80),map_total:all.length,size_bytes:all.reduce((s,n)=>s+n.size_bytes,0),file_count:all.length,scanned_files:result.scanned_files}});
    });
    await page.goto('http://127.0.0.1:8091/');
    await page.locator('#address').fill('demo.invalid');
    await page.locator('#username').fill('demo');
    await page.locator('#password').fill('demo-only');
    await page.locator('#connect-button').click();
    await page.locator('#analyze-button').click();
    await page.locator('#map-canvas [data-map-id="/volume1"]').waitFor();
    const frame = await page.locator('#map-canvas').boundingBox();
    const outerFrame = await page.locator('#space-map').boundingBox();
    await page.locator('#map-canvas [data-map-id="/volume1"]').click();
    await page.locator('#map-breadcrumbs [aria-current]').filter({hasText:'volume1'}).waitFor();
    await page.locator('#space-map').screenshot({path:'/tmp/dsm-treemap-desktop.png'});
    await page.locator('#map-canvas [data-map-id="/volume1/影视库"]').click();
    await page.locator('#map-canvas [data-map-id="/volume1/影视库/电影"]').click();
    let changedFrame = await page.locator('#map-canvas').boundingBox();
    assert.equal(changedFrame.width,frame.width);
    assert.equal(changedFrame.height,frame.height);
    const assertOuterFixed = async () => {
      const outer = await page.locator('#space-map').boundingBox();
      assert.equal(outer.width, outerFrame.width);
      assert.equal(outer.height, outerFrame.height);
    };
    await assertOuterFixed();
    await page.locator('#map-canvas [data-map-id="file:/volume1/影视库/电影/Interstellar.mkv"]').click();
    assert((await page.locator('#map-selection').innerText()).includes('Interstellar.mkv'));
    await assertOuterFixed();
    await page.waitForTimeout(1400); // A live poll must retain drill-down and selection.
    changedFrame = await page.locator('#map-canvas').boundingBox();
    assert.equal(changedFrame.width,frame.width);
    assert.equal(changedFrame.height,frame.height);
    assert((await page.locator('#map-breadcrumbs [aria-current]').innerText()).includes('电影'));
    assert((await page.locator('#map-selection').innerText()).includes('Interstellar.mkv'));
    await page.locator('#result-search').fill('Interstellar');
    assert.equal(await page.locator('#map-list button').count(),1);
    await page.locator('#result-search').fill('');
    await page.locator('#map-up').click();
    await page.locator('#map-up').click();
    // Long breadcrumbs and dense sidebars must not resize the outer map panel.
    const longPath='/volume1/文档/'+'很长的目录名称'.repeat(25);
    result.tree_directories.find(n=>n.path==='/volume1/文档').direct_size_bytes=30*GiB;
    result.tree_directories.push({path:longPath,size_bytes:10*GiB,file_count:1,direct_size_bytes:10*GiB,direct_file_count:1});
    for(let i=0;i<40;i++) result.tree_directories.push({path:'/volume1/文档/item'+i,size_bytes:GiB,file_count:1,direct_size_bytes:GiB,direct_file_count:1});
    await page.waitForTimeout(1600);
    await page.locator('#map-list [data-map-id="/volume1/文档"]').click();
    await assertOuterFixed();
    await page.locator('#map-list button').filter({hasText:'很长的目录名称'}).click();
    await assertOuterFixed();
    await page.locator('#map-list button').first().click();
    await assertOuterFixed();
    await page.locator('#map-breadcrumbs [data-map-id="/volume1"]').click();
    await page.setViewportSize({width:390,height:844});
    await page.locator('#space-map').screenshot({path:'/tmp/dsm-treemap-mobile.png'});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.getByRole('tab',{name:'大文件',exact:false}).filter({hasText:/^大文件\s/}).click();
    assert(await page.locator('#space-map').isHidden());
    assert(await page.locator('#result-content').isVisible());
    await page.getByRole('tab',{name:'空间地图'}).click();
    assert(await page.locator('#space-map').isVisible());
    await page.setViewportSize({width:1440,height:1100});
    result.tree_file_index=true;
    await page.waitForTimeout(1500);
    await page.locator('#map-list [data-map-id="/volume1/文档"]').click();
    await page.waitForFunction(()=>document.querySelector('#map-page').textContent.includes('全部'));
    await assertOuterFixed();
    await page.locator('#map-next').click();
    await page.waitForFunction(()=>document.querySelector('#map-page').textContent.startsWith('2 /'));
    assert(await page.locator('#map-list button').filter({hasText:'note'}).count()>0);
    await page.locator('#result-search').fill('note001');
    await page.waitForFunction(()=>document.querySelector('#map-page').textContent.includes('1 项（全部）'));
    await page.locator('#map-list button').filter({hasText:'note001.txt'}).click();
    assert((await page.locator('#map-selection').innerText()).includes('1 KB'));
    await assertOuterFixed();
    await page.locator('#result-search').fill('note000');
    await page.locator('#map-list button').filter({hasText:'note000.txt'}).waitFor();
    assert((await page.locator('#map-list').innerText()).includes('0 B'));
    await page.locator('#result-search').fill('');
    await page.waitForFunction(()=>document.querySelector('#map-page').textContent.includes('161 项'));
    await page.locator('#space-map').screenshot({path:'/tmp/dsm-small-files.png'});
    assert.deepEqual(errors,[]);
    console.log('PASS: fixed outer panel and canvas with long paths, dense lists, selection, live polls and navigation');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
