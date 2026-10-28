// In-memory fixtures only: never delete real user sessions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const makeSession = (id, name) => ({id,version:1,request:{company:{name},documents:[
  {document_id:'d1',filename:'资料.md',content:`${name}的私有资料`}]},
  messages:[{role:'user',content:`${name}的私有消息`}],effective_matches:[],runs:[],product_attempts:[],stale:false});
const sessions = new Map([['s1',makeSession('s1','企业甲')],['s2',makeSession('s2','企业乙')]]);
let deletes = 0, failNext = false;
(async () => {
  const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:960}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://fixture.test/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      const files = {'/':'chat.html','/assets/chat.js':'chat.js','/assets/chat.css':'chat.css'};
      if (files[pathname]) return route.fulfill({body:fs.readFileSync(path.join(root,'app/static',files[pathname]),'utf8'),
        contentType:pathname.endsWith('.js')?'text/javascript':pathname.endsWith('.css')?'text/css':'text/html'});
      if(pathname.endsWith('.svg')) return route.fulfill({status:204,body:''});
      const json = (body,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
      if(pathname==='/health') return json({});
      if(pathname==='/api/v1/financial-products') return json({products:[]});
      if(pathname==='/api/v1/knowledge') return json({items:[]});
      if(pathname==='/api/v1/sessions') return json({sessions:[...sessions.values()].map(s=>({id:s.id,company:s.request.company}))});
      const id = pathname.split('/').at(-1), session = sessions.get(id);
      if(!session) return json({error:{message:'会话不存在'}},404);
      if(request.method()==='DELETE') {
        assert.equal(request.postDataJSON().expected_version,session.version);
        if(failNext) {failNext=false;return json({error:{message:'会话已更新，请刷新后确认'}},409);}
        sessions.delete(id);deletes++;return json({deleted:true,session_id:id});
      }
      return json(session);
    });
    const idle = () => page.waitForFunction(()=>document.body.getAttribute('aria-busy')==='false');
    const openDelete = async name => {
      await page.getByRole('button',{name:`会话操作：${name}`,exact:true}).click();
      await page.getByRole('button',{name:`删除会话：${name}`,exact:true}).click();
      await page.locator('#delete-session-dialog').waitFor({state:'visible'});
    };
    await page.goto('http://fixture.test/');await idle();
    await page.locator('.session-open').filter({hasText:'企业甲'}).click();await idle();
    await page.locator('#chat-input').fill('即将删除的草稿');
    assert.equal(await page.locator('.session-delete:visible').count(),0);
    await openDelete('企业甲');
    await page.locator('#cancel-session-delete').click();await idle();
    assert.equal(deletes,0);
    assert.equal(await page.locator('#chat-input').inputValue(),'即将删除的草稿');
    await openDelete('企业甲');await page.keyboard.press('Escape');await idle();
    assert.equal(deletes,0);
    failNext=true;
    await openDelete('企业甲');await page.locator('#confirm-session-delete').click();await idle();
    assert.equal(deletes,0);
    assert.equal(await page.locator('#session-title').textContent(),'企业甲');
    await openDelete('企业乙');await page.locator('#confirm-session-delete').click();await idle();
    assert.equal(deletes,1);
    assert.equal(await page.locator('#session-title').textContent(),'企业甲');
    await openDelete('企业甲');await page.locator('#confirm-session-delete').click();await idle();
    assert.equal(deletes,2);
    assert.equal(await page.locator('.session-row').count(),0);
    assert.equal(await page.locator('#chat-input').inputValue(),'');
    assert.equal(await page.locator('#chat-send').isDisabled(),true);
    assert.equal(await page.locator('#manage-materials').isDisabled(),true);
    assert.equal(await page.locator('#messages').textContent(),'选择或创建企业会话。');
    assert.equal(await page.locator('#results .product').count(),0);
    assert.deepEqual(errors,[]);
    console.log('Session deletion: cancellation, conflict, other-session deletion and active-session cleanup passed.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
