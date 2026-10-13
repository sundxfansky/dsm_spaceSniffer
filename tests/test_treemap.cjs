const assert = require('node:assert/strict');
const {buildTree, layout, visibleItems, nestedLayout, kind} = require('../app/static/treemap.js');
const result = {tree_directories: [
  {path:'/volume1',size_bytes:100,file_count:4,direct_size_bytes:10,direct_file_count:1},
  {path:'/volume1/a',size_bytes:90,file_count:3,direct_size_bytes:30,direct_file_count:2},
  {path:'/volume1/a/b',size_bytes:60,file_count:1,direct_size_bytes:60,direct_file_count:1},
],large_files:[{path:'/volume1/a/b/video.mkv',size_bytes:60,link_count:2}]};
const {root,nodes} = buildTree(result);
assert.equal(root.size,100); // NOT 100 + 90 + 60.
assert.equal(root.count,4);
for (const n of nodes.values()) if(n.type==='folder') assert.equal(n.children.reduce((s,c)=>s+c.size,0), n.size);
assert.equal(nodes.get('small:/volume1/a').size,30);
assert.equal(nodes.get('small:/volume1/a').count,2);
assert.equal(kind(nodes.get('file:/volume1/a/b/video.mkv')),'video');
assert.equal(buildTree({}).root.size,0);
const all = buildTree({...result, tree_directories: result.tree_directories.map(row => row.path === '/volume1' ? {...row, direct_file_count:2,file_count:5} : row), tree_file_previews: [
  {path:'/volume1/a/b/video.mkv',size_bytes:60,link_count:2},
  {path:'/volume1/a/tiny.txt',size_bytes:1,link_count:1},
  {path:'/volume1/empty.txt',size_bytes:0,link_count:1},
]});
assert.equal(all.nodes.get('file:/volume1/a/tiny.txt').size,1);
assert.equal(all.nodes.get('file:/volume1/empty.txt').size,0);
assert.equal(all.nodes.get('/volume1/a/b').children.length,1); // Deduplicate large file and preview.
for (const n of all.nodes.values()) if(n.type==='folder') assert.equal(n.children.reduce((s,c)=>s+c.size,0), n.size);
const many = Array.from({length:1000},(_,i)=>({id:String(i),size:i+1,count:1,children:[]}));
const displayed = visibleItems(many);
assert.equal(displayed.length,80);
assert.equal(displayed.reduce((s,n)=>s+n.size,0),500500);
for(const [w,h] of [[1000,500],[300,390],[1,1]]) {
  const rects=layout(displayed,w,h);
  assert.equal(rects.length,80);
  for(const r of rects) {
    assert(r.width>0 && r.height>0);
    assert(r.x>=-1e-6 && r.y>=-1e-6 && r.x+r.width<=w+1e-6 && r.y+r.height<=h+1e-6);
    assert(Math.abs(r.width*r.height/(w*h)-r.node.size/500500)<1e-9);
  }
  for(let i=0;i<rects.length;i++) for(let j=i+1;j<rects.length;j++) {
    const a=rects[i],b=rects[j];
    assert(Math.min(a.x+a.width,b.x+b.width)-Math.max(a.x,b.x)<1e-6 || Math.min(a.y+a.height,b.y+b.height)-Math.max(a.y,b.y)<1e-6);
  }
}
assert.deepEqual(layout([{id:'zero',size:0}],100,100),[]);
const nested = nestedLayout(root.children, 1000, 540);
assert(nested[0].children[0].children.length > 0);
function checkBounds(rects, w, h) {
  for (const r of rects) {
    assert(r.x + r.width <= w + 1e-6 && r.y + r.height <= h + 1e-6);
    if (r.children.length) checkBounds(r.children, r.width - 4, r.height - 28);
  }
}
checkBounds(nested, 1000, 540);
checkBounds(nestedLayout(root.children, 300, 390), 300, 390);
console.log('PASS: hierarchy totals, small-file aggregation, layout proportions, no overlaps, bounded tiles');
