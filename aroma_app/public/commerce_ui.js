/* Local commerce screens. Existing app owns PIN, CSRF and maintenance guards. */
(() => {
  'use strict';
  const LABELS = {requests:'신청·주문',catalog:'체험·상품',slots:'일정'};
  const KINDS = {experience:'체험',finished_goods:'완성품',kit:'키트'};
  const REVIEW = {received:'접수됨',reviewing:'확인 중',closed:'확인 마침'};
  const ITEM_STATES = {inquiry:'상담 문의',ready:'운영 준비됨',paused:'잠시 중지'};
  const SLOT_STATES = {draft:'작성 중',ready:'운영 준비됨',paused:'잠시 중지'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const id = () => globalThis.crypto?.randomUUID?.() || `commerce_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  const nullableMoney = value => value === null || value === undefined ? '미정' : Number.isFinite(Number(value)) ? `${Number(value).toLocaleString('ko-KR')}원` : '금액 확인 필요';
  const stamp = value => { if (!value) return '날짜 미정'; const date = new Date(value); return Number.isNaN(date.getTime()) ? '날짜 확인 필요' : date.toLocaleString('ko-KR',{timeZone:'Asia/Seoul'}); };
  function toKoreaInput(value){if(!value)return '';const date=new Date(value);if(!Number.isFinite(date.getTime()))return '';return new Date(date.getTime()+9*60*60*1000).toISOString().slice(0,16);}
  function fromKoreaInput(value){if(!value)return null;if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value))throw new Error('날짜와 시간을 확인해 주세요.');const converted=`${value}:00+09:00`;if(toKoreaInput(converted)!==value)throw new Error('날짜와 시간을 확인해 주세요.');return converted;}
  const input = (label,name,value='',type='text',hint='',extra='') => `<div class="field"><label for="commerce-${esc(name)}">${esc(label)}</label><input id="commerce-${esc(name)}" name="${esc(name)}" type="${esc(type)}" value="${esc(value)}" ${extra}>${hint?`<p class="field-hint">${esc(hint)}</p>`:''}</div>`;
  const select = (label,name,value,options,hint='') => `<div class="field"><label for="commerce-${esc(name)}">${esc(label)}</label><select id="commerce-${esc(name)}" name="${esc(name)}">${options.map(([key,text])=>`<option value="${esc(key)}" ${key===value?'selected':''}>${esc(text)}</option>`).join('')}</select>${hint?`<p class="field-hint">${esc(hint)}</p>`:''}</div>`;
  const numberOrNull = value => value === '' || value === null || value === undefined ? null : Number(value);
  const note = (text,warning=false) => `<div class="commerce-notice ${warning?'warning':''}" role="status"><p>${esc(text)}</p></div>`;

  function create(hooks) {
    const state = {root:null,view:'requests',status:null,items:[],requests:[],slots:[],busy:false,error:'',filter:'todo',query:'',epoch:0,pending:null};
    let activeWrite=false;
    const token = () => hooks.getAdminToken?.() || '';
    const block = () => hooks.getBlockReason?.() || '';
    const field = name => document.getElementById(`commerce-${name}`)?.value ?? '';
    const clearClosedDialog = () => { if(!document.getElementById('message-dialog')?.open)document.getElementById('dialog-content')?.replaceChildren?.(); };
    const wipe = () => { state.items=[]; state.requests=[]; state.slots=[]; state.pending=null; state.error=''; state.query=''; state.filter='todo'; };
    const authOptions = body => ({headers:{'X-Admin-Token':token()},...(body===undefined?{}:{method:'POST',body})});
    const itemName = request => request.details?.offering_snapshot?.title || state.items.find(x=>x.id===request.offering_id)?.title || '항목 확인 필요';
    const itemKind = request => request.details?.offering_snapshot?.offering_kind || state.items.find(x=>x.id===request.offering_id)?.offering_kind;
    const productQuantities = request => {const items=Array.isArray(request.details?.items)?request.details.items:[];return items.length?items.map(item=>`${String(item?.title||'상품')} · ${Number.isSafeInteger(item?.quantity)&&item.quantity>0?`${item.quantity}개`:'수량 아직 확인 전'}`).join(', '):'구매 수량 아직 확인 전';};
    const authenticated = () => !!token();

    async function checkedDialog(title,body,actions,validate){
      const waiting=hooks.dialog(title,body,actions);
      const save=document.getElementById('dialog-actions')?.querySelector?.('button');
      const check=event=>{let message;try{message=validate();}catch(error){message=error.message;}
        if(!message)return;event.preventDefault();event.stopImmediatePropagation();
        const content=document.getElementById('dialog-content');let errorBox=content?.querySelector('[data-commerce-form-error]');
        if(!errorBox&&content){errorBox=document.createElement('p');errorBox.dataset.commerceFormError='true';errorBox.className='commerce-form-error';errorBox.setAttribute('role','alert');content.prepend(errorBox);}
        if(errorBox){errorBox.textContent=message;errorBox.scrollIntoView({block:'nearest'});}
      };
      save?.addEventListener('click',check,true);
      try{return await waiting;}finally{save?.removeEventListener('click',check,true);}
    }

    function connectionPanel() {
      if (!state.status) return note(state.error?'접수 연결 상태를 확인하지 못했어요. 다시 확인해 주세요.':'접수 연결 상태를 확인하고 있어요.');
      const connected=state.status.connection_status==='connected'&&state.status.server_registered===true;
      const pending=Number(state.status.pending_operations||0);
      return `<section class="commerce-connection"><div><strong>${connected?'접수 연결 상태 확인 필요':'외부 미연결'}</strong><p>${connected?'접수 반영 결과를 별도로 확인해 주세요.':'고객 페이지의 새 신청을 아직 가져오지 않습니다.'}</p></div><div><span class="commerce-count">접수·확인 중 ${Number(state.status.open_requests||0)}건 (취소 포함)</span><span class="commerce-count ${pending?'waiting':''}">전송 대기 ${pending}건</span></div>${pending?'<p class="commerce-pending">이 컴퓨터에서 바꾼 상태가 고객 접수 서버에 아직 반영되지 않았습니다.</p>':''}<p class="field-hint">상품·일정 수정은 이 컴퓨터의 검토용 초안입니다. 고객 공개·예약·결제 완료로 간주하지 않습니다.</p></section>`;
    }

    function toolbar() {
      return `<div class="commerce-toolbar"><button class="btn secondary" data-commerce-action="refresh" ${state.busy?'disabled':''}>목록 다시 보기</button>${authenticated()?`<button class="btn secondary" data-commerce-action="sync" ${state.busy?'disabled':''}>새 접수 확인</button><button class="btn secondary" data-commerce-action="lock" ${state.busy?'disabled':''}>관리자 잠금</button>`:`<button class="btn" data-commerce-action="unlock" ${state.busy?'disabled':''}>관리자 확인</button>`}</div>`;
    }

    // Adapted from the attached TeacherDesk: show outstanding work first and keep
    // search/filter choices in this session. Reading the list changes no record.
    const REQUEST_FILTERS = [['todo','확인할 문의'],['all','전체'],['received','접수됨'],['reviewing','확인 중'],['closed','확인 마침']];
    const needsReview = request => ['received','reviewing'].includes(request.review_status) && request.booking_status!=='cancelled';
    const searchText = value => String(value ?? '').trim().toLocaleLowerCase('ko-KR');
    function requestView() {
      const query=searchText(state.query);
      const counts={all:state.requests.length,todo:state.requests.filter(needsReview).length};
      for(const key of ['received','reviewing','closed'])counts[key]=state.requests.filter(request=>request.review_status===key).length;
      const shown=state.requests.filter(request=>
        (state.filter==='all'||(state.filter==='todo'?needsReview(request):request.review_status===state.filter)) &&
        (!query||searchText(itemName(request)).includes(query)||searchText(request.id).includes(query))
      ).slice().sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')));
      return {shown,counts};
    }

    function requestList(shown) {
      if(!state.requests.length)return '<div class="empty-state">아직 이 목록에 접수가 없어요.<br>고객 접수 연결을 준비하거나 필요한 문의를 직접 적어 주세요.</div>';
      if(!shown.length)return `<div class="empty-state">${searchText(state.query)?'이 검색어와 선택한 상태에 맞는 문의가 없어요.<br>검색어를 지우거나 전체 목록을 확인해 주세요.':state.filter==='todo'?'현재 확인할 문의가 없어요.<br>전체 목록에서 확인을 마쳤거나 취소한 문의도 볼 수 있습니다.':'선택한 확인 상태에 맞는 문의가 없어요.<br>전체 목록에서도 살펴볼 수 있습니다.'}</div>`;
      return `<div class="commerce-list">${shown.map(r=>`<article class="commerce-card"><div class="commerce-card-heading"><h3>${esc(itemName(r))}</h3><span class="badge warn">${esc(REVIEW[r.review_status]||'상태 확인 필요')}</span></div><p>${esc(KINDS[itemKind(r)]||'항목')} · ${itemKind(r)==='experience'?`참여 인원 ${esc(r.participants)}명`:esc(productQuantities(r))}</p><p class="muted">${esc(stamp(r.created_at))} · ${r.source==='server'?'접수 사본':'직접 기록'}</p><p class="commerce-request-id">신청번호 ${esc(r.id||'확인 필요')}</p><p>${r.booking_status==='cancelled'?'문의 취소':'상담·일정 문의'} · 결제 준비 중</p><div class="button-row"><button class="btn secondary" data-commerce-action="detail" data-id="${esc(r.id)}">내용 보기</button><button class="btn" data-commerce-action="review" data-id="${esc(r.id)}" ${state.busy?'disabled':''}>확인 상태 바꾸기</button></div></article>`).join('')}</div>`;
    }

    function requestResultText(view) {
      return `보이는 문의 ${view.shown.length}건 · 전체 ${view.counts.all}건`;
    }

    function requestCards() {
      const view=requestView();
      return `<div class="commerce-section-head"><h2>문의와 접수 목록</h2><button class="btn" data-commerce-action="new-request" ${state.busy||!state.items.length?'disabled':''}>문의 직접 적기</button></div><p class="field-hint">확인할 문의는 접수됨·확인 중인 문의입니다. 취소한 문의는 전체 목록에서 확인해 주세요.</p><div class="commerce-filters" aria-label="확인 상태로 보기">${REQUEST_FILTERS.map(([key,title])=>`<button class="btn ${state.filter===key?'':'secondary'}" data-commerce-action="filter" data-value="${key}" aria-pressed="${state.filter===key}">${title} <span class="commerce-filter-count">${view.counts[key]}건</span></button>`).join('')}</div><div class="commerce-search"><label for="commerce-request-search">문의 찾기</label><input id="commerce-request-search" data-commerce-search type="search" value="${esc(state.query)}" maxlength="120" placeholder="체험·상품 이름 또는 신청번호" autocomplete="off" aria-describedby="commerce-search-help"><p id="commerce-search-help" class="field-hint">항목 이름과 신청번호로 찾습니다.</p></div><p class="commerce-result-count" data-commerce-result-count role="status" aria-live="polite" aria-atomic="true">${requestResultText(view)}</p><div data-commerce-request-results>${requestList(view.shown)}</div>`;
    }

    function updateRequestResults() {
      const results=state.root?.querySelector('[data-commerce-request-results]');
      if(!results)return;
      const view=requestView();
      results.innerHTML=requestList(view.shown);
      const counter=state.root.querySelector('[data-commerce-result-count]');
      if(counter)counter.textContent=requestResultText(view);
    }

    function search(event) {
      if(!event.target.matches?.('[data-commerce-search]')||!authenticated()||state.view!=='requests')return;
      state.query=event.target.value.slice(0,120);
      // Keep the actual input node, including Korean composition and caret.
      updateRequestResults();
    }

    function requestFocus() {
      const active=document.activeElement;
      if(!state.root?.contains(active))return null;
      if(active.matches?.('[data-commerce-search]'))return {search:true,start:active.selectionStart,end:active.selectionEnd,direction:active.selectionDirection};
      if(active.dataset?.commerceAction==='filter')return {filter:active.dataset.value};
      return null;
    }

    function restoreRequestFocus(focus) {
      if(!focus||!authenticated()||state.view!=='requests')return;
      if(focus.search){const input=state.root.querySelector('[data-commerce-search]');if(input){input.focus({preventScroll:true});if(Number.isInteger(focus.start)&&Number.isInteger(focus.end))input.setSelectionRange(focus.start,focus.end,focus.direction||'none');}}
      else if(focus.filter){const button=[...state.root.querySelectorAll('[data-commerce-action="filter"]')].find(node=>node.dataset.value===focus.filter);button?.focus({preventScroll:true});}
    }

    function catalogCards() {
      const cards=state.items.map(item=>`<article class="commerce-card"><p class="eyebrow">${esc(KINDS[item.offering_kind]||'항목')}</p><h3>${esc(item.title)}</h3><span class="badge warn">${esc(ITEM_STATES[item.state]||'상담 문의')} · 로컬 검토용</span><dl class="commerce-facts"><div><dt>가격</dt><dd>${esc(nullableMoney(item.price===undefined?item.unit_price:item.price))}</dd></div><div><dt>재고</dt><dd>${item.stock===null||item.stock===undefined?'미정':esc(item.stock)}</dd></div><div><dt>고객 공개</dt><dd>외부 반영 안 됨</dd></div><div><dt>수업 프로그램</dt><dd>${item.program_id?'직접 연결한 후보':'연결 안 함'}</dd></div></dl><button class="btn secondary" data-commerce-action="edit-item" data-id="${esc(item.id)}" ${state.busy?'disabled':''}>내용·가격 수정</button></article>`).join('');
      const candidates=!state.items.length?`<div class="empty-state">등록한 항목이 없어요.<br>아래 이름을 확인한 뒤 상담용 초안으로 추가할 수 있습니다.</div><div class="commerce-catalog">${['감정향수','천연비누','석고방향제','천연생활용품'].map(title=>`<article class="commerce-card"><h3>${title}</h3><p>체험 후보 · 아직 등록 안 함</p><button class="btn secondary" data-commerce-action="template-item" data-title="${title}" ${state.busy?'disabled':''}>이 이름으로 추가</button></article>`).join('')}</div>`:'';
      return `<div class="commerce-section-head"><h2>체험과 상품 목록</h2><button class="btn" data-commerce-action="new-item" ${state.busy?'disabled':''}>체험·상품 추가</button></div>${note('확인한 가격·재고만 적어 주세요. 모르면 비워 두면 미정으로 저장됩니다.')}<div class="commerce-catalog">${cards}</div>${candidates}<p class="field-hint">기존 수업 후보를 연결해도 판매·운영 승인이 정해지지는 않습니다. 상태와 가격을 바꾸어도 고객에게 자동 공개하지 않습니다.</p>`;
    }

    function slotCards() {
      return `<div class="commerce-section-head"><h2>체험 일정 목록</h2><button class="btn" data-commerce-action="new-slot" ${state.busy||!state.items.some(x=>x.offering_kind==='experience')?'disabled':''}>일정 추가</button></div>${note('입력한 날짜·시간은 한국시간입니다. 이 컴퓨터의 검토용 일정이며 고객 예약을 자동 승인하지 않습니다.',true)}${state.slots.length?`<div class="commerce-list">${state.slots.map(s=>`<article class="commerce-card"><h3>${esc(state.items.find(x=>x.id===s.offering_id)?.title||'체험 확인 필요')}</h3><span class="badge warn">${esc(SLOT_STATES[s.state||s.status]||'작성 중')} · 로컬 검토용</span><dl class="commerce-facts"><div><dt>시작 (한국시간)</dt><dd>${s.starts_at?esc(stamp(s.starts_at)):'미정'}</dd></div><div><dt>종료 (한국시간)</dt><dd>${s.ends_at?esc(stamp(s.ends_at)):'미정'}</dd></div><div><dt>정원</dt><dd>${s.capacity===null||s.capacity===undefined?'미정':esc(s.capacity)}</dd></div></dl><button class="btn secondary" data-commerce-action="edit-slot" data-id="${esc(s.id)}" ${state.busy?'disabled':''}>일정 수정</button><p class="field-hint">저장만으로 예약이나 자리를 확보하지 않습니다.</p></article>`).join('')}</div>`:'<div class="empty-state">아직 일정이 없어요.<br>체험을 고른 뒤 날짜·시간·정원을 준비할 수 있습니다.</div>'}`;
    }

    function render() {
      if (!state.root?.isConnected) return;
      const focus=requestFocus();
      if (!authenticated()) { state.items=[]; state.requests=[]; state.slots=[]; state.query=''; state.filter='todo'; }
      const blocked=block();
      state.root.innerHTML=`<div class="commerce-page"><div class="page-intro"><p class="eyebrow">이 컴퓨터에서 확인해요</p><h1>${LABELS[state.view]}</h1><p>${state.view==='requests'?'신청 내용을 보고 확인 상태를 정리해요.':state.view==='catalog'?'상담할 체험과 준비할 상품을 정리해요.':'체험별로 운영할 일정을 준비해요.'}</p></div>${connectionPanel()}${toolbar()}${blocked?note(blocked,true):''}${state.error?`<div class="commerce-notice warning" role="alert"><p>${esc(state.error)}</p></div>`:''}${state.pending?`<div class="commerce-notice warning"><p>저장 결과를 확인하지 못했어요. 같은 내용을 다시 확인하거나 목록을 새로 보세요.</p><button class="btn secondary" data-commerce-action="retry" ${state.busy?'disabled':''}>같은 저장 다시 확인</button></div>`:''}${!authenticated()?'<section class="panel commerce-locked"><h2>관리자 확인이 필요해요</h2><p>신청 정보와 체험·상품은 PIN 확인 후 볼 수 있습니다.</p><button class="btn" data-commerce-action="unlock">관리자 확인</button></section>':state.view==='requests'?requestCards():state.view==='catalog'?catalogCards():slotCards()}<p class="commerce-footer-note">카드 결제는 연결하지 않았습니다. 견적·계획서 저장도 예약이나 주문 승인으로 간주하지 않습니다.</p></div>`;
      restoreRequestFocus(focus);
    }

    function presentError(error) {
      if ([401,403].includes(error?.status)) { wipe();hooks.onAuthError?.();state.error='관리자 확인이 끝났거나 만료됐어요. PIN으로 다시 확인해 주세요.'; }
      else if (error?.data?.code==='commerce_not_connected') state.error='아직 고객 접수 서버와 연결되지 않았어요. 새 신청을 가져오거나 확인 내용을 보내지 않았습니다.';
      else if (error?.status===409) state.error=error.message||'다른 저장 내용과 차이가 있어요. 목록을 새로 보고 다시 확인해 주세요.';
      else state.error=error?.message||'확인하지 못했어요. 앱 연결을 확인하고 다시 시도해 주세요.';
    }

    async function refresh() {
      const epoch=state.epoch;
      state.busy=true;state.error='';render();
      try {
        const status=await hooks.api('/api/commerce/status');
        if (epoch!==state.epoch) return;
        state.status=status;
        if (authenticated()&&!block()) {
          const paths=state.view==='requests'?['catalog','requests']:state.view==='slots'?['catalog','slots']:['catalog'];
          const replies=await Promise.all(paths.map(path=>hooks.api(`/api/commerce/${path}`,authOptions())));
          if (epoch!==state.epoch) return;
          paths.forEach((path,index)=>{const response=replies[index];if(path==='catalog')state.items=Array.isArray(response.items)?response.items:[];if(path==='requests')state.requests=Array.isArray(response.requests)?response.requests:[];if(path==='slots')state.slots=Array.isArray(response.slots)?response.slots:[];});
        }
      } catch(error) { if(epoch===state.epoch)presentError(error); }
      finally { if(epoch===state.epoch){state.busy=activeWrite;render();} }
    }

    async function unlock() {
      if (block()) { hooks.toast?.(block(),true);return; }
      const granted=await hooks.ensureAdmin('신청·주문 관리자 확인','이 컴퓨터의 신청 정보와 체험·상품을 관리하려면 PIN을 확인합니다.');
      if(granted)await refresh();
    }

    async function mutate(path,payload,retry=false) {
      if(state.busy||activeWrite)return;
      if(block()){hooks.toast?.(block(),true);return;}
      if(!authenticated()){await unlock();if(!authenticated())return;}
      if(!retry&&state.pending){hooks.toast?.('앞선 저장 결과부터 확인해 주세요.',true);return;}
      state.pending={path,payload};activeWrite=true;state.busy=true;state.error='';render();
      try {
        await hooks.api(path,authOptions(payload));
        state.pending=null;hooks.toast?.('이 컴퓨터에 저장했어요. 외부 반영과 예약·결제는 별도입니다.');
      } catch(error) {
        if(error?.status)state.pending=null;
        presentError(error);
      } finally {activeWrite=false;state.busy=false;}
      await refreshKeepingError();
    }

    async function refreshKeepingError(){const message=state.error;await refresh();if(message&&!state.error){state.error=message;render();}}

    async function itemEditor(item,presetTitle='') {
      const programs=hooks.getPrograms?.()||[];
      const linked=select('수업 후보 연결 (선택)','program',item?.program_id||'',[['','연결하지 않음'],...programs.map(p=>[p.id,p.title||p.name||'수업 후보'])],'수업 연결은 판매나 예약 승인이 아닙니다.');
      const choice=await checkedDialog(item?'체험·상품 수정':'체험·상품 추가',`<div class="commerce-dialog">${input('이름','title',item?.title||presetTitle,'text','고객에게 안내할 이름을 적어 주세요.','maxlength="80" autocomplete="off"')}${select('분류','kind',item?.offering_kind||'experience',Object.entries(KINDS))}${input('가격 (원)','price',item?.price===undefined?item?.unit_price??'':item.price??'','number','아직 모르면 비워 두세요.','min="0" step="1" inputmode="numeric"')}${input('재고 수량','stock',item?.stock??'','number','아직 모르면 비워 두세요.','min="0" step="1" inputmode="numeric"')}${select('운영 상태','item-state',item?.state||'inquiry',Object.entries(ITEM_STATES))}${linked}${note('이 컴퓨터의 검토용 초안으로 저장합니다. 가격·상태를 바꾸어도 고객에게 공개하거나 예약·결제를 승인하지 않습니다.',true)}</div>`,[{label:'검토 초안 저장',value:'save'},{label:'취소',value:'cancel',secondary:true}],()=>{
        if(!field('title').trim())return '이름을 입력해 주세요.';
        if([numberOrNull(field('price')),numberOrNull(field('stock'))].some(v=>v!==null&&(!Number.isSafeInteger(v)||v<0)))return '가격과 재고는 0 이상의 정수로 적거나 비워 주세요.';
      });
      if(choice!=='save')return;
      const title=field('title').trim();if(!title){hooks.toast?.('이름을 입력해 주세요.',true);return;}
      const price=numberOrNull(field('price')),stock=numberOrNull(field('stock'));
      if([price,stock].some(value=>value!==null&&(!Number.isSafeInteger(value)||value<0))){hooks.toast?.('가격과 재고는 0 이상의 정수로 적거나 비워 주세요.',true);return;}
      const payload={request_id:id(),title,offering_kind:field('kind'),program_id:field('program')||null,price,stock,state:field('item-state')};
      if(item)Object.assign(payload,{id:item.id,revision:item.revision});
      await mutate('/api/commerce/catalog',payload);
    }

    async function slotEditor(slot=null) {
      const experiences=state.items.filter(x=>x.offering_kind==='experience');
      if(!experiences.length)return;
      const choice=await checkedDialog(slot?'체험 일정 수정':'체험 일정 추가',`<div class="commerce-dialog">${select('어떤 체험인가요?','offering',slot?.offering_id||'', [['','체험을 골라 주세요'],...experiences.map(x=>[x.id,x.title])])}${input('시작 날짜·시간 (한국시간)','starts-at',toKoreaInput(slot?.starts_at),'datetime-local','미정이면 비워 두세요.','step="60"')}${input('종료 날짜·시간 (한국시간)','ends-at',toKoreaInput(slot?.ends_at),'datetime-local','미정이면 비워 두세요.','step="60"')}${input('정원 (명)','capacity',slot?.capacity??'','number','미정이면 비워 두세요.','min="1" max="10000" step="1" inputmode="numeric"')}${select('일정 상태','slot-state',slot?.state||'draft',Object.entries(SLOT_STATES),'운영 준비됨으로 저장하려면 시작 시간과 정원이 필요합니다.')}${note('한국시간으로 입력한 내용만 저장합니다. 이 일정은 로컬 검토용이며 고객 예약을 자동 승인하지 않습니다.',true)}</div>`,[{label:'검토 일정 저장',value:'save'},{label:'취소',value:'cancel',secondary:true}],()=>{
        if(!field('offering'))return '체험을 골라 주세요.';
        const start=fromKoreaInput(field('starts-at')),end=fromKoreaInput(field('ends-at')),capacity=numberOrNull(field('capacity'));
        if(capacity!==null&&(!Number.isInteger(capacity)||capacity<1||capacity>10000))return '정원은 1~10,000명으로 적거나 비워 주세요.';
        if(start&&end&&new Date(end)<=new Date(start))return '종료 시간을 시작 시간보다 뒤로 입력해 주세요.';
        if(field('slot-state')==='ready'&&(!start||capacity===null))return '운영 준비됨으로 저장하려면 시작 시간과 정원을 입력해 주세요.';
      });
      if(choice!=='save')return;const offering=field('offering');if(!offering){hooks.toast?.('체험을 골라 주세요.',true);return;}
      let starts_at,ends_at;try{starts_at=fromKoreaInput(field('starts-at'));ends_at=fromKoreaInput(field('ends-at'));}catch(error){hooks.toast?.(error.message,true);return;}
      const capacity=numberOrNull(field('capacity')),slotState=field('slot-state');
      if(capacity!==null&&(!Number.isInteger(capacity)||capacity<1||capacity>10000)){hooks.toast?.('정원은 1~10,000명으로 적거나 비워 주세요.',true);return;}
      if(starts_at&&ends_at&&new Date(ends_at)<=new Date(starts_at)){hooks.toast?.('종료 시간을 시작 시간보다 뒤로 입력해 주세요.',true);return;}
      if(slotState==='ready'&&(!starts_at||capacity===null)){hooks.toast?.('운영 준비됨으로 저장하려면 시작 시간과 정원을 입력해 주세요.',true);return;}
      const payload={request_id:id(),offering_id:offering,starts_at,ends_at,capacity,state:slotState};
      if(slot)Object.assign(payload,{id:slot.id,revision:slot.revision});
      await mutate('/api/commerce/slots',payload);
    }

    async function newRequest() {
      const selectedItem=()=>state.items.find(x=>x.id===field('offering'));
      const waiting=checkedDialog('문의 직접 적기',`<div class="commerce-dialog">${select('체험 또는 상품','offering','',[['','항목을 골라 주세요'],...state.items.map(x=>[x.id,`${KINDS[x.offering_kind]} · ${x.title}`])])}<div id="commerce-participants-field" hidden>${input('체험 참여 인원','participants','','number','체험에 참여할 인원을 확인해 적어 주세요.','min="1" max="10000" step="1" inputmode="numeric"')}</div><p id="commerce-product-quantity" class="field-hint" hidden>상품 구매 수량은 아직 확인 전으로 저장합니다. 참여 인원을 구매 수량으로 사용하지 않습니다.</p>${input('어른 담당 이름 (선택)','name','','text','','maxlength="60" autocomplete="off"')}${input('어른 담당 전화번호','phone','','tel','전화번호나 이메일 중 하나만 있으면 됩니다.','maxlength="40" autocomplete="off"')}${input('어른 담당 이메일','email','','email','','maxlength="254" autocomplete="off"')}${note('필요한 어른 연락처만 적어 주세요. 학생·건강정보는 수집하지 않습니다. 이 기록은 상담 문의이며 예약·판매·결제 확정이 아닙니다.',true)}</div>`,[{label:'문의 저장',value:'save'},{label:'취소',value:'cancel',secondary:true}],()=>{
        const participants=numberOrNull(field('participants')),phone=field('phone').trim(),email=field('email').trim();
        const item=selectedItem();
        if(!item)return '체험 또는 상품을 골라 주세요.';
        if(item.offering_kind==='experience'&&(!Number.isInteger(participants)||participants<1||participants>10000))return '체험 참여 인원을 확인해 주세요.';
        if(!phone&&!email)return '전화번호 또는 이메일 중 하나를 적어 주세요.';
        if(phone&&!/^[+0-9() .-]{5,40}$/.test(phone))return '전화번호 형식을 확인해 주세요.';
        if(email&&!/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(email))return '이메일 형식을 확인해 주세요.';
      });
      const selector=document.getElementById('commerce-offering');
      const showParticipants=()=>{const item=selectedItem(),experience=item?.offering_kind==='experience';const box=document.getElementById('commerce-participants-field'),quantity=document.getElementById('commerce-product-quantity'),participants=document.getElementById('commerce-participants');if(box)box.hidden=!experience;if(quantity)quantity.hidden=!item||experience;if(participants){participants.disabled=!experience;if(!experience)participants.value='';}};
      selector?.addEventListener('change',showParticipants);showParticipants();
      let choice;try{choice=await waiting;}finally{selector?.removeEventListener('change',showParticipants);}
      if(choice!=='save'){clearClosedDialog();return;}
      const offering=field('offering'),item=selectedItem(),participants=item?.offering_kind==='experience'?numberOrNull(field('participants')):null;
      const adult_contact={name:field('name').trim()||null,phone:field('phone').trim()||null,email:field('email').trim()||null};
      clearClosedDialog();
      if(!item||(item.offering_kind==='experience'&&(!Number.isInteger(participants)||participants<1||participants>10000))||(!adult_contact.phone&&!adult_contact.email)){hooks.toast?.('항목·체험 인원·전화번호 또는 이메일을 확인해 주세요.',true);return;}
      await mutate('/api/commerce/requests',{request_id:id(),offering_id:offering,adult_contact,participants});
    }

    async function requestDetail(r) {
      const c=r.adult_contact||{};
      await hooks.dialog('신청·주문 내용',`<div class="commerce-dialog"><h3>${esc(itemName(r))}</h3><dl class="commerce-facts"><div><dt>이름</dt><dd>${esc(c.name||'입력 안 함')}</dd></div><div><dt>전화</dt><dd>${esc(c.phone||'입력 안 함')}</dd></div><div><dt>이메일</dt><dd>${esc(c.email||'입력 안 함')}</dd></div><div><dt>${itemKind(r)==='experience'?'참여 인원':'구매 수량'}</dt><dd>${itemKind(r)==='experience'?`${esc(r.participants)}명`:esc(productQuantities(r))}</dd></div><div><dt>확인 상태</dt><dd>${esc(REVIEW[r.review_status]||'확인 필요')}</dd></div><div><dt>예약</dt><dd>${r.booking_status==='cancelled'?'문의 취소':'상담·일정 문의'}</dd></div><div><dt>총금액</dt><dd>${esc(nullableMoney(r.details?.total))}</dd></div><div><dt>결제</dt><dd>준비 중</dd></div></dl>${note('연락처는 이 컴퓨터에서만 확인합니다. 예약·결제 완료로 사용하지 마세요.',true)}</div>`);
      clearClosedDialog();
    }

    async function reviewRequest(r) {
      const choice=await hooks.dialog('확인 상태 바꾸기',`<div class="commerce-dialog"><h3>${esc(itemName(r))}</h3>${select('문의 확인 상태','review',r.review_status,Object.entries(REVIEW))}${select('상담 문의 상태','booking',r.booking_status,[['inquiry','상담·일정 문의'],['cancelled','문의 취소']])}${note('이 컴퓨터의 확인 상태만 바꿉니다. 외부 접수 사본은 전송 대기로 남을 수 있습니다. 예약 확정·결제 확인은 아직 준비 중입니다.',true)}</div>`,[{label:'상태 저장',value:'save'},{label:'취소',value:'cancel',secondary:true}]);
      if(choice!=='save')return;
      await mutate('/api/commerce/requests/review',{request_id:id(),id:r.id,revision:r.revision,review_status:field('review'),booking_status:field('booking'),payment_status:'not_configured'});
    }

    async function sync() {
      if(block()){hooks.toast?.(block(),true);return;}
      if(!authenticated())return;
      state.busy=true;state.error='';render();
      try {await hooks.api('/api/commerce/sync',authOptions({confirm:true}));await refresh();}
      catch(error){presentError(error);state.busy=false;render();}
      finally{state.busy=false;render();}
    }

    async function click(event) {
      const button=event.target.closest('[data-commerce-action]');
      if(!button||button.disabled||!state.root?.contains(button))return;
      const action=button.dataset.commerceAction;
      if(action==='lock'){state.epoch++;wipe();hooks.onLock?.();render();return;}
      if(action==='refresh'){await refresh();return;}
      if(action==='unlock'){await unlock();return;}
      if(!authenticated()){await unlock();return;}
      if(action==='filter'){if(REQUEST_FILTERS.some(([key])=>key===button.dataset.value))state.filter=button.dataset.value;render();return;}
      if(action==='sync'){await sync();return;}
      if(action==='retry'&&state.pending){await mutate(state.pending.path,state.pending.payload,true);return;}
      if(action==='new-item'){await itemEditor(null);return;}
      if(action==='template-item'){await itemEditor(null,button.dataset.title||'');return;}
      if(action==='edit-item'){const item=state.items.find(x=>x.id===button.dataset.id);if(item)await itemEditor(item);return;}
      if(action==='new-slot'){await slotEditor();return;}
      if(action==='edit-slot'){const slot=state.slots.find(x=>x.id===button.dataset.id);if(slot)await slotEditor(slot);return;}
      if(action==='new-request'){await newRequest();return;}
      const request=state.requests.find(x=>x.id===button.dataset.id);
      if(request&&action==='detail')await requestDetail(request);
      if(request&&action==='review')await reviewRequest(request);
    }

    return {
      mount(root,view='requests'){if(state.root){state.root.removeEventListener('click',click);state.root.removeEventListener('input',search);}state.epoch++;state.root=root;state.view=LABELS[view]?view:'requests';if(!authenticated())wipe();root.addEventListener('click',click);root.addEventListener('input',search);render();refresh();},
      unmount(){state.epoch++;state.root?.removeEventListener('click',click);state.root?.removeEventListener('input',search);state.root=null;state.busy=activeWrite;},
      reset(){state.epoch++;wipe();state.status=null;state.busy=activeWrite;if(state.root?.isConnected)render();}
    };
  }
  globalThis.AromaCommerce={create,toKoreaInput,fromKoreaInput};
})();
