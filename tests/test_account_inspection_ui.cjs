const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webui/templates/account_inspections.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const empty = {active:null, total:0, items:[]};
const active = {active:{id:1}, total:1, items:[{id:1, archived:'only', status:'running', workers:4}]};
async function flush() {for (let i = 0; i < 12; i++) await Promise.resolve();}
function harness(storage = new Map(), deferPreview = false) {
  const elements = new Map(), requests = [], previews = [], timers = new Map(), toasts = [];
  let nextTimer = 0, accountsReloaded = 0;
  const confirmations = [], selected = new Set([1,2]);
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value:'4', open:false, disabled:false, textContent:'', innerHTML:'',
      scrollTop:0, querySelector(){return null;}, closest(){return this;},
      classList:{hidden:false, contains(){return this.hidden;}}, showModal(){this.open=true;}, close(){this.open=false;},
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    document:{hidden:false, getElementById:element}, SHOW_ARCHIVED_ACCOUNTS:true, AbortController,
    localStorage:{getItem:key=>storage.get(key), setItem:(key,value)=>storage.set(key,value), removeItem:key=>storage.delete(key)},
    ACCOUNT_SELECTED:selected, confirm(message){confirmations.push(message);return true;},
    setTimeout(callback){const id=++nextTimer;timers.set(id,callback);return id;},
    clearTimeout(id){timers.delete(id);}, setInterval(){},
    showToast(message){toasts.push(message);}, loadAccounts(){accountsReloaded++;}, loadSummary(){},
    esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));},
    api(url,options){
      if(url.endsWith("/preview") && !deferPreview)return Promise.resolve({ok:true,total_count:4});
      return new Promise((resolve,reject)=>{
      const request={url,options,settled:false};
      request.resolve=value=>{request.settled=true;resolve(value);};
      request.reject=error=>{request.settled=true;reject(error);};
      (url.endsWith("/preview") ? previews : requests).push(request);
      options?.signal?.addEventListener('abort',()=>request.reject(new Error('aborted')));
    });},
  });
  const run=source=>vm.runInContext(source,context);
  run(script);
  return {element,requests,previews,timers,toasts,run,confirmations,selected,storage,get accountsReloaded(){return accountsReloaded;},
    async resolve(index,value){requests[index].resolve(value);await flush();},
    async reject(index,error){requests[index].reject(error);await flush();},
    async expire(){for(const callback of [...timers.values()])callback();await flush();},
  };
}
(async()=>{
  // 范围以打开弹窗时为准；启动前/期间旧 GET 不能解锁，丢弃后立即补读。
  const h=harness();
  h.run('startAccountInspection()'); await flush();
  assert.equal(h.element('inspectionScopeLabel').textContent,'已归档');
  h.run('SHOW_ARCHIVED_ACCOUNTS=false');
  const submit=h.run('submitAccountInspection({preventDefault(){}})');
  assert.deepEqual(JSON.parse(h.requests[1].options.body),{workers:4,archived:'only',mode:'inspection',scope:'all'});
  await h.resolve(0,empty);
  assert.equal(h.element('btnStartInspection').disabled,true);
  h.run('refreshInspectionHistory()');
  await h.resolve(1,{ok:true}); await submit;
  assert.equal(h.run('inspectionActive'),true);
  await h.resolve(2,empty);
  assert.equal(h.element('btnStartInspection').disabled,false); // 运行中按钮用于重新打开进度。
  assert.equal(h.run('inspectionActive'),true);
  assert.equal(h.requests.length,4); // 不丢失启动完成时的刷新请求。
  await h.resolve(3,active);
  assert.match(h.element('inspectionHistoryContent').innerHTML,/已归档/);
  h.run('refreshInspectionHistory()');
  await h.reject(4,new Error('network failure'));
  assert.equal(h.run('inspectionLoading'),false);
  h.run('refreshInspectionHistory()'); await h.expire();
  assert.equal(h.run('inspectionLoading'),false);
  assert.equal(h.timers.size,0);
  h.run('refreshInspectionHistory()'); await h.resolve(6,empty);
  assert.equal(h.element('btnStartInspection').disabled,false);
  assert.equal(h.accountsReloaded,1);
  assert.match(h.element('inspectionHistoryContent').innerHTML,/暂无任务记录/);
  h.run('refreshInspectionHistory()');
  await h.resolve(7,{active:null,total:1,items:[{
    id:8,archived:'only',status:'<img src=x>',workers:8,created_at:'2026-10-17T17:20:10',
    checked_count:44,total_count:40,skipped_count:2,invalid_count:5,
    live_success_count:3,live_failed_count:2,plan_success_count:2,plan_failed_count:1,
    error:'<script>alert(1)</script>',
  }]});
  const markup=h.element('inspectionHistoryContent').innerHTML;
  for(const pattern of [/46 \/ 40/,/跳过 2 个/,/width:100%/,/inspection-pill--unknown/,/&lt;script&gt;/])assert.match(markup,pattern);
  assert.doesNotMatch(markup,/<script>|<img/);
  assert.equal(h.element('inspectionRefresh').disabled,false);

  // POST 超时不能永久卡住，也不能假定服务端未创建任务而允许盲目重提。
  const timeout=harness();await timeout.resolve(0,empty);
  timeout.run('startAccountInspection()'); await flush();
  const pending=timeout.run('submitAccountInspection({preventDefault(){}})');
  await timeout.expire();await pending;
  assert.equal(timeout.run('inspectionStarting'),false);
  assert.equal(timeout.run('inspectionUncertain'),true);
  assert.equal(timeout.element('btnStartInspection').disabled,true);
  assert.match(timeout.element('btnStartInspection').textContent,/待确认/);
  timeout.run('submitAccountInspection({preventDefault(){}})');
  assert.equal(timeout.requests.length,3);
  await timeout.resolve(2,active);
  assert.equal(timeout.run('inspectionUncertain'),false);
  assert.equal(timeout.run('inspectionActive'),true);

  // 切换页码时遇到正在加载的请求，完成后应立即读取目标页。
  const pages=harness();await pages.resolve(0,empty);
  pages.run('inspectionPage=2;refreshInspectionHistory()');
  pages.run('openInspectionHistory()');
  await pages.resolve(1,empty);
  assert.equal(pages.requests[2].url,'/api/accounts/inspections?page=1');
  await pages.resolve(2,empty);
  assert.equal(pages.run('inspectionLoading'),false);

  // POST 断连后的旧 GET 不得错误解除“状态待确认”。
  const uncertain=harness();await uncertain.resolve(0,empty);
  uncertain.run('startAccountInspection()'); await flush();
  const failure=uncertain.run('submitAccountInspection({preventDefault(){}})');
  uncertain.run('refreshInspectionHistory()');
  await uncertain.reject(1,new Error('connection lost'));await failure;
  await uncertain.resolve(2,empty);
  assert.equal(uncertain.run('inspectionUncertain'),true);
  await uncertain.resolve(3,empty);
  assert.equal(uncertain.run('inspectionUncertain'),false);
  assert.equal(uncertain.element('btnStartInspection').disabled,false);
  assert.equal(uncertain.timers.size,0);

  // 失败详情入口、按任务隔离、分页、旧响应隔离和转义。
  assert.match(markup,/openInspectionFailures\(8\)/);
  const details=harness();await details.resolve(0,empty);
  details.run('openInspectionFailures(8)');
  assert.equal(details.requests[1].url,'/api/accounts/inspections/8/failures?page=1');
  details.run('openInspectionFailures(9)');
  await details.resolve(1,{task:{archived:'0'},total:1,details_available:true,items:[{account_id:1,email:'stale@example.test',error:'stale'}]});
  assert.doesNotMatch(details.element('inspectionFailuresContent').innerHTML,/stale@example/);
  assert.equal(details.requests[2].url,'/api/accounts/inspections/9/failures?page=1');
  await details.resolve(2,{task:{archived:'only'},total:21,details_available:true,items:[{
    account_id:2,email:'<img src=x>',status:'deactivated',error:'<script>alert(1)</script>',failed_at:'2026-10-18T12:00:00',
  }]});
  assert.match(details.element('inspectionFailuresContent').innerHTML,/账号已废/);
  assert.match(details.element('inspectionFailuresContent').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(details.element('inspectionFailuresContent').innerHTML,/<script>|<img/);
  assert.equal(details.element('inspectionFailuresNext').disabled,false);
  details.run('changeInspectionFailuresPage(1)');
  assert.equal(details.requests[3].url,'/api/accounts/inspections/9/failures?page=2');
  await details.resolve(3,{task:{archived:'only'},total:21,details_available:true,items:[{account_id:25,email:'last@example.test',error:'mailbox unavailable'}]});
  assert.match(details.element('inspectionFailuresContent').innerHTML,/last@example/);
  assert.equal(details.element('inspectionFailuresNext').disabled,true);
  details.run('openInspectionFailures(10)');
  assert.equal(details.element('inspectionFailuresNext').disabled,true);
  assert.doesNotMatch(details.element('inspectionFailuresSubtitle').textContent,/已归档/);
  await details.resolve(4,{task:{archived:'0'},total:0,details_available:false,items:[]});
  assert.match(details.element('inspectionFailuresContent').innerHTML,/旧任务仅保留汇总/);
  details.run('openInspectionFailures(11)');
  await details.expire();
  assert.match(details.element('inspectionFailuresContent').innerHTML,/暂时无法读取/);
  assert.equal(details.element('inspectionFailuresRefresh').disabled,false);
  details.run('refreshInspectionFailures()');
  await details.resolve(6,{task:{archived:'0'},total:0,details_available:true,items:[]});
  assert.match(details.element('inspectionFailuresContent').innerHTML,/暂无失败或已废跳过记录/);
  assert.equal(details.timers.size,0);

  // 同页 DOM 不变时跳过重建；有变化保留阅读位置，翻页重置。
  details.run(`(() => {
    let markup = 'old', writes = 0;
    let wrap = {scrollLeft:0};
    const body = {scrollTop:0};
    const content = {
      closest:()=>body, querySelector:()=>wrap,
      get innerHTML(){return markup;},
      set innerHTML(value){markup=value;writes++;wrap={scrollLeft:0};body.scrollTop=0;},
    };
    renderInspectionContent(content, 'first', 'page:1');
    wrap.scrollLeft=180;body.scrollTop=600;
    renderInspectionContent(content, 'first', 'page:1');
    if(writes!==1 || wrap.scrollLeft!==180 || body.scrollTop!==600)throw Error('unchanged render reset');
    renderInspectionContent(content, 'changed', 'page:1');
    if(writes!==2 || wrap.scrollLeft!==180 || body.scrollTop!==600)throw Error('refresh scroll reset');
    renderInspectionContent(content, 'second', 'page:2');
    if(wrap.scrollLeft!==0 || body.scrollTop!==0)throw Error('page scroll not reset');
  })()`);

  // 一键删除已废：确认范围、启动时快照、防重复提交和选择清理。
  const deletion=harness();await deletion.resolve(0,empty);
  const deleting=deletion.run('deleteDeactivatedAccounts()');
  assert.match(deletion.confirmations[0],/已归档/);
  assert.match(deletion.confirmations[0],/不限当前页/);
  assert.deepEqual(JSON.parse(deletion.requests[1].options.body),{archived:'only'});
  deletion.run('SHOW_ARCHIVED_ACCOUNTS=false;deleteDeactivatedAccounts()');
  assert.equal(deletion.requests.length,2);
  assert.equal(deletion.element('btnDeleteDeactivatedAccounts').disabled,true);
  await deletion.resolve(1,{ok:true,deleted_count:2});await deleting;
  assert.equal(deletion.selected.size,0);
  assert.equal(deletion.element('btnDeleteDeactivatedAccounts').disabled,false);
  const retried=deletion.run('deleteDeactivatedAccounts()');
  assert.deepEqual(JSON.parse(deletion.requests[2].options.body),{archived:'0'});
  await deletion.reject(2,new Error('offline'));await retried;
  assert.match(deletion.toasts.at(-1),/核实/);
  assert.equal(deletion.element('btnDeleteDeactivatedAccounts').disabled,false);
  deletion.run('confirm=()=>false;deleteDeactivatedAccounts()');
  assert.equal(deletion.requests.length,3);
  // 已废跳过能独立进入详情，但不冒充查活失败。
  deletion.run('refreshInspectionHistory()');
  await deletion.resolve(3,{active:null,total:1,items:[{id:6,status:'completed',total_count:2,
    checked_count:0,skipped_count:2,deactivated_skipped_count:2,live_failed_count:0}]});
  assert.match(deletion.element('inspectionHistoryContent').innerHTML,/已废 2/);
  assert.match(deletion.element('inspectionHistoryContent').innerHTML,/失败详情（2）/);
  deletion.run('openInspectionFailures(6)');
  await deletion.resolve(4,{task:{archived:'0'},details_available:true,total:2,
    items:[{account_id:1,email:'dead@example.test',status:'deactivated_skipped',error:'已废跳过'}]});
  assert.match(deletion.element('inspectionFailuresContent').innerHTML,/已废跳过/);

  const initial=harness();await initial.reject(0,new Error('offline'));
  assert.match(initial.element('inspectionHistoryContent').innerHTML,/暂时无法读取/);
  assert.equal(initial.element('inspectionRefresh').disabled,false);

  // 两种一键模式无需勾选；仅提交模式、范围与线程，运行中点击入口只恢复进度。
  for (const mode of ['plan','live']) {
    const bulk=harness();await bulk.resolve(0,empty);bulk.selected.clear();
    bulk.run(`startAccountInspection('${mode}')`); await flush();
    assert.match(bulk.element('inspectionStartTitle').textContent,mode==='plan'?/查套餐/:/查活刷新 AT/);
    const starting=bulk.run('submitAccountInspection({preventDefault(){}})');
    assert.deepEqual(JSON.parse(bulk.requests[1].options.body),{workers:4,archived:'only',mode,scope:'all'});
    await bulk.resolve(1,{ok:true,task:{id:42,mode}});await starting;
    const ongoing={active:{id:42,mode},total:1,items:[{id:42,mode,status:'running',total_count:10,
      processed_count:3,checked_count:8,skipped_count:1,plan_failed_count:1,failure_count:1}]};
    await bulk.resolve(2,ongoing);
    assert.match(bulk.element('inspectionHistoryContent').innerHTML,/3 \/ 10 · 30%/);
    assert.match(bulk.element('inspectionHistoryContent').innerHTML,/openInspectionFailures\(42\)/);
    bulk.element('inspectionHistoryDialog').close();
    bulk.run('refreshInspectionHistory()');await bulk.resolve(3,ongoing);
    assert.equal(bulk.element('inspectionHistoryDialog').open,false); // 后台轮询不反复弹出。
    bulk.run(`startAccountInspection('${mode}')`); await flush();
    assert.equal(bulk.element('inspectionHistoryDialog').open,true);
    assert.equal(bulk.requests.filter(r=>r.options?.method==='POST').length,1);
    await bulk.resolve(4,ongoing);
    bulk.element('inspectionHistoryDialog').close();
    bulk.element('tab-accounts').classList.hidden=true;
    bulk.run('refreshInspectionHistory()');await bulk.resolve(5,ongoing);
    assert.equal(bulk.element('inspectionHistoryDialog').open,false);
    bulk.element('tab-accounts').classList.hidden=false;
    bulk.run('restoreAccountTaskProgress()');await bulk.resolve(6,ongoing);
    assert.equal(bulk.element('inspectionHistoryDialog').open,true);

    // 整页重新加载后由服务端恢复；关闭时完成的任务也能恢复查看一次结果。
    const reload=harness(bulk.storage);await reload.resolve(0,ongoing);
    assert.equal(reload.element('inspectionHistoryDialog').open,true);
    reload.element('inspectionHistoryDialog').close();
    const done={active:null,total:1,items:[{...ongoing.items[0],status:'completed',processed_count:10}]};
    reload.run('refreshInspectionHistory()');await reload.resolve(1,done);
    assert.equal(reload.element('inspectionHistoryDialog').open,false);
    const finished=harness(reload.storage);await finished.resolve(0,done);
    assert.equal(finished.element('inspectionHistoryDialog').open,true);
    assert.equal(finished.run('inspectionResume'),false);
    assert.match(finished.element('inspectionHistoryContent').innerHTML,/10 \/ 10 · 100%/);
    finished.element('inspectionHistoryDialog').close();
    finished.run('refreshInspectionHistory()');await finished.resolve(1,done);
    assert.equal(finished.element('inspectionHistoryDialog').open,false);
  }

  const background=harness();background.element('tab-accounts').classList.hidden=true;
  await background.resolve(0,active);
  assert.equal(background.element('inspectionHistoryDialog').open,false);
  background.element('tab-accounts').classList.hidden=false;
  background.run('restoreAccountTaskProgress()');await background.resolve(1,active);
  assert.equal(background.element('inspectionHistoryDialog').open,true);

  // 返回页面时仍在途的旧 GET 不能消耗恢复请求，必须等待新的快照。
  const returning=harness();returning.element('tab-accounts').classList.hidden=true;
  returning.element('tab-accounts').classList.hidden=false;
  returning.run('restoreAccountTaskProgress()');
  await returning.resolve(0,empty);
  assert.equal(returning.run('inspectionRestoreRequested'),true);
  await returning.resolve(1,active);
  assert.equal(returning.element('inspectionHistoryDialog').open,true);

  // 曾翻到旧历史页后启动失败，恢复进度必须读取第一页，才能显示当前任务。
  const recovery=harness();await recovery.resolve(0,empty);
  recovery.run("inspectionPage=2;startAccountInspection('plan')"); await flush();
  const recovering=recovery.run('submitAccountInspection({preventDefault(){}})');
  await recovery.reject(1,new Error('connection lost'));await recovering;
  assert.equal(recovery.requests[2].url,'/api/accounts/inspections?page=1');
  await recovery.resolve(2,active);
  assert.equal(recovery.element('inspectionHistoryDialog').open,true);
  assert.equal(recovery.element('inspectionStartDialog').open,false);
  assert.match(recovery.element('inspectionHistoryContent').innerHTML,/#1/);
  // 范围预览的旧响应不可启用新范围；零账号和失败均不能提交。
  const selection=harness(new Map(),true);await selection.resolve(0,empty);
  selection.run("startAccountInspection('live')");
  assert.equal(selection.element('btnConfirmInspection').disabled,true);
  selection.element('inspectionSelectionScope').value='failed';
  selection.run('changeInspectionSelection()');
  selection.previews[0].resolve({ok:true,total_count:99});await flush();
  assert.equal(selection.element('btnConfirmInspection').disabled,true);
  selection.previews[1].resolve({ok:true,total_count:0});await flush();
  assert.equal(selection.element('btnConfirmInspection').disabled,true);
  await selection.run('submitAccountInspection({preventDefault(){}})');
  assert.equal(selection.requests.length,1);
  selection.run('previewInspectionSelection()');
  selection.previews[2].reject(new Error('preview failed'));await flush();
  assert.equal(selection.element('btnConfirmInspection').disabled,true);
  selection.run('previewInspectionSelection()');
  selection.previews[3].resolve({ok:true,total_count:2});await flush();
  assert.equal(selection.element('btnConfirmInspection').disabled,false);
  const startSelected=selection.run('submitAccountInspection({preventDefault(){}})');
  assert.deepEqual(JSON.parse(selection.requests[1].options.body),{workers:4,archived:'only',mode:'live',scope:'failed'});
  await selection.resolve(1,{ok:true});await startSelected;

  // 当前筛选和勾选以打开弹窗时为准，筛选不是当前页 ID 列表。
  const filters=harness();await filters.resolve(0,empty);
  filters.run("getAccountsQuery=()=> 'needle'; getAccountsPlanFilter=()=> 'plus'; getAccountsTotpFilter=()=> 'enabled'");
  filters.element('dateFromAccountsV2').value='2026-11-01';
  filters.element('dateToAccountsV2').value='2026-11-03';
  filters.run("startAccountInspection('plan')");await flush();
  filters.run("getAccountsQuery=()=> 'changed'; ACCOUNT_SELECTED.clear()");
  filters.element('inspectionSelectionScope').value='filtered';
  filters.run('changeInspectionSelection()');await flush();
  assert.deepEqual(JSON.parse(filters.run('JSON.stringify(inspectionSelectionOptions())')),{
    archived:'only',mode:'plan',scope:'filtered',filters:{q:'needle',plan:'plus',codex_status:'',totp_status:'enabled',date_from:'2026-11-01',date_to:'2026-11-03'},
  });
  filters.element('inspectionSelectionScope').value='selected';
  filters.run('changeInspectionSelection()');await flush();
  assert.deepEqual(JSON.parse(filters.run('JSON.stringify(inspectionSelectionOptions())')).account_ids,[1,2]);

  // 历史的两个阶段独立重试；按钮只接受已结束、有明细的任务。
  const retry=harness();await retry.resolve(0,empty);
  retry.run('refreshInspectionHistory()');
  await retry.resolve(1,{active:null,total:1,items:[{id:77,mode:'live',archived:'0',status:'completed',
    failure_details_available:true,live_failed_count:1,plan_failed_count:1,selection:{scope:'failed'}}]});
  const retryHtml=retry.element('inspectionHistoryContent').innerHTML;
  assert.match(retryHtml,/retryInspectionFailures\(77, 'live', '0'\)/);
  assert.match(retryHtml,/retryInspectionFailures\(77, 'plan', '0'\)/);
  retry.run("retryInspectionFailures(77,'plan','0')");await flush();
  assert.equal(retry.element('inspectionScopeLabel').textContent,'未归档');
  assert.equal(retry.element('inspectionSelectionScope').value,'history_failed');
  assert.match(retry.element('inspectionSelectionHint').textContent,/#77/);
  const retrySubmit=retry.run('submitAccountInspection({preventDefault(){}})');
  assert.deepEqual(JSON.parse(retry.requests[2].options.body),{workers:4,archived:'0',mode:'plan',scope:'history_failed',source_inspection_id:77});
  await retry.resolve(2,{ok:true});await retrySubmit;
  assert.equal(retry.run("inspectionRetryButton({id:1,status:'running',failure_details_available:true,plan_failed_count:1},'plan')"),'');
  assert.equal(retry.run("inspectionRetryButton({id:1,status:'completed',plan_failed_count:1},'plan')"),'');
  console.log('巡检 UI 验收通过：筛选/勾选/最近失败/历史分阶段重试、预览竞态与空范围防重、旧响应隔离、超时、分页、统计与转义。');
})().catch(error=>{console.error(error);process.exitCode=1;});
