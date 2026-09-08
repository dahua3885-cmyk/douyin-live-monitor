(() => {
  'use strict';
  const L=window.LiveMonitor,$=id=>document.getElementById(id),e=L.escape;
  const busy=async(button,fn)=>{button.disabled=true;try{return await fn();}catch(error){L.toast(error.message,true);throw error;}finally{button.disabled=false;}};
  function selection(){
    const rooms=L.model.state?.rooms||[];
    for(const id of L.selectedRooms)if(!rooms.some(r=>r.id===id))L.selectedRooms.delete(id);
    $('selection-count').textContent=L.selectedRooms.size;
    $('selection-actions').hidden=!L.selectedRooms.size;$('selection-hint').hidden=Boolean(L.selectedRooms.size);
    $('select-all-rooms').checked=rooms.length>0&&rooms.every(r=>L.selectedRooms.has(r.id));
  }
  $('room-rows').addEventListener('change',event=>{
    const id=event.target.dataset.roomSelect;if(!id)return;
    if(event.target.checked)L.selectedRooms.add(id);else L.selectedRooms.delete(id);
    selection();L.renderRooms();
  });
  $('select-all-rooms').addEventListener('change',event=>{
    L.selectedRooms.clear();if(event.target.checked)for(const room of L.model.state?.rooms||[])L.selectedRooms.add(room.id);
    selection();L.renderRooms();
  });
  setInterval(selection,4000);
  function showCustomLimit(id){
    const input=$(id+'-custom'),custom=$(id).value==='custom';
    input.hidden=!custom;input.disabled=!custom;input.required=custom;
  }
  function limitMinutes(id){return Number($(id).value==='custom'?$(id+'-custom').value:$(id).value);}
  $('record-limit').addEventListener('change',()=>showCustomLimit('record-limit'));
  let settingsKey='';
  function renderInlineSettings(){
    const r=L.model.state?.settings?.recording;if(!r)return;
    const key=JSON.stringify([r.segment_minutes,r.record_limit_minutes,r.record_quality,r.convert_mp4]);
    if($('record-settings-form').dataset.dirty==='true'||key===settingsKey)return;
    settingsKey=key;$('record-settings-form').dataset.dirty='false';
    for(const [id,k] of [['record-segment','segment_minutes'],['record-quality','record_quality']])$(id).value=String(r[k]);
    const limit=String(r.record_limit_minutes);$('record-limit').value=[...$('record-limit').options].some(o=>o.value===limit)?limit:'custom';
    $('record-limit-custom').value=r.record_limit_minutes||180;showCustomLimit('record-limit');
    $('record-mp4').checked=r.convert_mp4;$('record-settings-error').textContent='';
  }
  window.addEventListener('live-monitor-settings',renderInlineSettings);renderInlineSettings();
  let pendingRecordRoom=null;
  L.openRecordingSettings=(roomId=null)=>{
    pendingRecordRoom=roomId;
    renderInlineSettings();
    $('record-settings-description').textContent=roomId?'首次录像，请先选择保存位置并确认设置。保存后开启该账号录像。':'所有账号共用，新增账号自动沿用。';
    $('record-settings-form').querySelector('button[type="submit"]').textContent=roomId?'保存并开启录像':'保存设置';
    $('record-settings-dialog').showModal();
  };
  $('record-settings-button').addEventListener('click',()=>L.openRecordingSettings());
  $('record-settings-dialog').addEventListener('close',()=>{
    pendingRecordRoom=null;L.recordingFolderDraft=null;
    $('record-folder-label').textContent=L.model.state?.settings?.recordings_dir?`统一保存到：${L.model.state.settings.recordings_dir}`:'首次录制前选择一次保存文件夹，所有账号共用。';
    $('record-settings-form').dataset.dirty='false';settingsKey='';renderInlineSettings();
  });
  $('record-settings-form').addEventListener('input',()=>{$('record-settings-form').dataset.dirty='true';$('record-settings-error').textContent='有未保存的修改';});
  $('record-settings-form').addEventListener('submit',async event=>{
    event.preventDefault();
    if(pendingRecordRoom&&!L.recordingFolderDraft&&!L.model.state?.settings?.recordings_dir){$('record-settings-error').textContent='首次录像必须先选择视频保存文件夹。';$('change-record-folder').focus();return;}
    const roomId=pendingRecordRoom,folder=L.recordingFolderDraft,button=event.submitter;
    button.disabled=true;$('record-settings-form').dataset.saving='true';
    try{
      await L.request('/api/recording-settings',{method:'POST',body:{segment_minutes:Number($('record-segment').value),record_limit_minutes:limitMinutes('record-limit'),record_quality:$('record-quality').value,convert_mp4:$('record-mp4').checked}});
      if(folder)await L.request('/api/recording-folder',{method:'POST',body:{path:folder}});
      if(roomId&&pendingRecordRoom===roomId&&$('record-settings-dialog').open)await L.request(`/api/rooms/${encodeURIComponent(roomId)}`,{method:'PATCH',body:{record_enabled:true,enabled:true}});
      L.recordingFolderDraft=null;$('record-settings-form').dataset.dirty='false';settingsKey='';await L.syncState();$('record-settings-dialog').close();L.toast(roomId?'设置已保存，自动录像已开启。':'全局录像设置已保存，所有账号新启动的录像统一使用。');
    }catch(error){$('record-settings-error').textContent=error.message;}finally{$('record-settings-form').dataset.saving='false';button.disabled=!L.model.connected;}
  });
  $('add-file').addEventListener('change',async()=>{const file=$('add-file').files[0];if(file){if(file.size>200000){L.toast('文件超过 200 KB',true);return;}$('room-url').value=await file.text();}});
  for(const [id,enabled] of [['bulk-start',true],['bulk-pause',false]])$(id).addEventListener('click',async()=>{
    if(!L.selectedRooms.size)return;
    try{await busy($(id),async()=>{await L.request('/api/rooms/bulk',{method:'POST',body:{ids:[...L.selectedRooms],changes:{enabled}}});await L.syncState();L.toast(enabled?'选中账号已开始监控。':'选中账号已暂停监控。');});}catch(_){}
  });
  $('bulk-remove').addEventListener('click',async()=>{
    if(!L.selectedRooms.size)return;
    try{await busy($('bulk-remove'),async()=>{const result=await L.request('/api/rooms/bulk-remove',{method:'POST',body:{ids:[...L.selectedRooms]}});L.selectedRooms.clear();await L.syncState();selection();L.toast(`已移除 ${result.removed} 个账号，历史资料保留。`);});}catch(_){}
  });
  $('bulk-settings-button').addEventListener('click',()=>{
    selection();if(!L.selectedRooms.size){L.toast('先勾选要设置的账号。');return;}
    $('bulk-settings-form').reset();$('bulk-settings-error').textContent='';$('bulk-settings-count').textContent=`将修改 ${L.selectedRooms.size} 个已勾选账号。`;$('bulk-settings-dialog').showModal();
  });
  $('bulk-settings-form').addEventListener('submit',async event=>{
    event.preventDefault();const changes={};
    for(const [id,key] of [['bulk-enabled','enabled'],['bulk-record','record_enabled'],['bulk-speech','transcribe_enabled']])if($(id).value)changes[key]=$(id).value==='true';
    if((changes.record_enabled||changes.transcribe_enabled)&&changes.enabled!==false)changes.enabled=true;
    if(changes.enabled===false&&(changes.record_enabled||changes.transcribe_enabled)){$('bulk-settings-error').textContent='暂停监控和开启录像/转写不能同时设置。';return;}
    if($('bulk-group').value.trim())changes.group_name=$('bulk-group').value.trim();
    if(!Object.keys(changes).length){$('bulk-settings-error').textContent='请至少选择一项要修改的设置。';return;}
    if(changes.record_enabled&&!L.model.state?.settings?.recordings_dir){$('bulk-settings-error').textContent='请先完成全局录像设置，完成后再次应用批量设置。';L.openRecordingSettings();return;}
    const button=event.submitter;button.disabled=true;
    try{const result=await L.request('/api/rooms/bulk',{method:'POST',body:{ids:[...L.selectedRooms],changes}});$('bulk-settings-dialog').close();await L.syncState();L.toast(`已更新 ${result.updated} 个账号。`);}
    catch(error){$('bulk-settings-error').textContent=error.message;}finally{button.disabled=false;}
  });

  let activeArchive=null,archiveItems=[],currentSpeech=[],archiveSequence=0;
  async function loadArchives(){
    const id=$('archive-room-filter').value;
    const result=await L.request('/api/archives'+(id?`?room_id=${encodeURIComponent(id)}`:''));archiveItems=result.items;
    $('archive-list').innerHTML=archiveItems.length?archiveItems.map(r=>`<button type="button" class="archive-item ${r.id===activeArchive?'active':''}" data-archive="${e(r.id)}"><strong>${e(r.name)}</strong><span>${e(new Date(r.first_seen).toLocaleDateString('zh-CN'))} · ${e(L.time(r.first_seen,true))}—${e(L.time(r.last_seen,true))}</span><small>${r.legacy?'当日历史汇总':r.state==='ended'?'已结束':'最近检测在播'} · ${r.video_count?'含录像':'仅话术'}</small></button>`).join(''):'<p class="muted">尚未积累直播档案。监控到开播后会自动建立。</p>';
    if(activeArchive&&!archiveItems.some(r=>r.id===activeArchive))activeArchive=null;
    if(!activeArchive&&archiveItems.length)await showArchive(archiveItems[0].id);
  }
  $('archive-button').addEventListener('click',async()=>{
    const filter=$('archive-room-filter');filter.innerHTML='<option value="">全部账号（含已移除）</option>'+(L.model.state?.rooms||[]).map(r=>`<option value="${e(r.id)}">${e(L.roomName(r))}</option>`).join('');
    $('archive-dialog').showModal();try{await loadArchives();}catch(error){$('archive-list').textContent=error.message;}
  });
  $('archive-room-filter').addEventListener('change',()=>{activeArchive=null;loadArchives().catch(error=>L.toast(error.message,true));});
  $('archive-refresh').addEventListener('click',async()=>{try{await loadArchives();if(activeArchive)await showArchive(activeArchive);}catch(error){L.toast(error.message,true);}});
  $('archive-list').addEventListener('click',event=>{const button=event.target.closest('[data-archive]');if(button)showArchive(button.dataset.archive).catch(error=>L.toast(error.message,true));});
  function smallChart(points){
    const valid=points.filter(p=>p.online!==null&&p.online!==undefined&&p.status==='live');if(!valid.length)return '<p class="muted">该场次没有可用在线人数数据。</p>';
    const min=Math.min(...valid.map(p=>Date.parse(p.observed_at))),max=Math.max(...valid.map(p=>Date.parse(p.observed_at))),peak=Math.max(...valid.map(p=>p.online),1);
    const dots=valid.map(p=>{const x=10+680*(Date.parse(p.observed_at)-min)/Math.max(1,max-min),y=115-100*p.online/peak;return `<circle cx="${x}" cy="${y}" r="3" fill="#226648"><title>${e(L.fullTime(p.observed_at))}：${p.online} 人</title></circle>`;}).join('');
    return `<svg viewBox="0 0 700 130" role="img" aria-label="本场在线人数采样点，空白处不补线"><path d="M10 15V115H690" stroke="#dce3d9" fill="none"/>${dots}</svg><p class="muted">仅展示实际采样点；悬停可查看时间与人数。</p>`;
  }
  async function showArchive(id){
    activeArchive=id;document.querySelectorAll('[data-archive]').forEach(b=>b.classList.toggle('active',b.dataset.archive===id));const sequence=++archiveSequence;
    const data=await L.request(`/api/archives/${encodeURIComponent(id)}`);if(sequence!==archiveSequence)return;
    const media=data.media.map(m=>`<div class="archive-media"><span>${e(L.fullTime(m.captured_at))} · ${(m.duration/60).toFixed(1)} 分钟 · ${e(m.filename)}<small>${e(({ready:'MP4 已就绪',saved:'TS 已保存',pending:'等待生成 MP4',converting:'正在生成 MP4',error:'MP4 转换失败'})[m.state]||m.state)}${m.error?' · '+e(m.error):''}</small></span><div><button class="text-button" type="button" data-open-media-folder="${e(m.id)}">打开位置</button>${m.playable?`<button type="button" class="text-button" data-play-media="${e(m.id)}">播放</button><a class="text-button" href="/api/media/${e(m.id)}?download=1" download>MP4</a>`:''}<a class="text-button" href="/api/media/${e(m.id)}?original=1" download>原 TS</a>${['saved','error'].includes(m.state)?`<button type="button" class="text-button" data-retry-media="${e(m.id)}">生成 / 重试 MP4</button>`:''}</div></div>`).join('');
    const stats=`<div class="archive-stats"><span>已存音频 <strong>${(data.speech_seconds/60).toFixed(1)} 分钟</strong></span><span>已归档录像 <strong>${(data.video_seconds/60).toFixed(1)} 分钟</strong></span><span>采样峰值在线 <strong>${data.peak?data.peak.online:'—'}</strong></span><span>采样平均在线 <strong>${data.observed_average_online??'—'}</strong></span></div>`;
    const gaps=`<details class="archive-gaps"><summary>采集中断 / 异常 ${data.gaps.length} 处</summary>${data.gaps.map(g=>`<p>${e(L.fullTime(g.started_at))} → ${g.ended_at?e(L.fullTime(g.ended_at)):'尚未确认恢复'}：${e(g.reason)}</p>`).join('')||'<p>没有记录到异常；不代表监控前的内容也已采集。</p>'}</details>`;
    const members=data.members?`<details class="archive-gaps"><summary>原始采音记录 ${data.members.length} 次（已合并阅读）</summary>${data.members.map(m=>`<p>${e(L.fullTime(m.first_seen))}—${e(L.fullTime(m.last_seen))}</p>`).join('')}</details>`:'';
    $('archive-detail').innerHTML=`<div class="archive-title"><div><h3>${e(data.name)}</h3><p>${e(L.fullTime(data.first_seen))} 至 ${e(L.fullTime(data.last_seen))}</p></div><span class="count-tag">${data.legacy?'历史汇总':data.state==='ended'?'已结束':'最近检测在播'}</span></div>${data.legacy?'<p class="archive-context-note">同一主播当日的旧采音记录已合并阅读；原始记录和中断保留。历史资料不足以确认真实场次。</p>':''}<div class="archive-search"><input id="archive-search-input" type="search" placeholder="搜索话术：报名、课程、资料…" aria-label="搜索本场话术"><button id="archive-search-button" class="button secondary small" type="button">搜索话术</button><button id="analyze-archive" class="button primary small" type="button">生成结构拆解</button></div><nav class="archive-tabs" aria-label="历史内容"><button type="button" data-archive-tab="speech" class="active">话术全文</button><button type="button" data-archive-tab="video">录像回看</button><button type="button" data-archive-tab="data">人数与中断</button><button type="button" data-archive-tab="analysis">结构拆解</button></nav><div id="archive-player-zone" hidden><video id="archive-player" controls preload="metadata" hidden></video><audio id="archive-audio" controls preload="metadata" hidden></audio><p id="archive-play-note" class="muted"></p></div><section data-archive-panel="speech"><div id="archive-speech"></div></section><section data-archive-panel="video" hidden>${media||'<p class="archive-empty-note">这次记录只采集了话术，没有录像。以后开启列表中的录像开关即可保存视频。</p>'}</section><section data-archive-panel="data" hidden>${stats}<h4>人数走势</h4>${smallChart(data.points)}${gaps}${members}</section><section data-archive-panel="analysis" hidden><a class="text-button" href="/api/archives/${e(id)}/analysis.md" download>导出拆解</a><div id="archive-analysis"><p class="archive-empty-note">点击上方“生成结构拆解”，按时间查看内容讲解、互动、转化及对应原话。</p></div></section>`;
    document.querySelectorAll('[data-archive-tab]').forEach(button=>button.addEventListener('click',()=>switchArchiveTab(button.dataset.archiveTab)));
    $('archive-player').addEventListener('error',()=>{$('archive-play-note').textContent='浏览器无法播放此编码，请下载 MP4 或原 TS 用本机播放器打开。';});
    $('archive-search-button').addEventListener('click',()=>loadSpeech().catch(error=>L.toast(error.message,true)));
    $('archive-search-input').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();loadSpeech().catch(error=>L.toast(error.message,true));}});
    $('analyze-archive').addEventListener('click',async()=>{
      switchArchiveTab('analysis');const button=$('analyze-archive');button.disabled=true;
      try{const requested=activeArchive;const result=await L.request(`/api/archives/${encodeURIComponent(requested)}/analyze`,{method:'POST',body:{}});if(activeArchive===requested)renderAnalysis(result);}catch(error){L.toast(error.message,true);}finally{button.disabled=false;}
    });
    await loadSpeech();
  }
  async function loadSpeech(){
    const id=activeArchive,query=$('archive-search-input').value.trim();
    switchArchiveTab('speech');const result=await L.request(`/api/archives/${encodeURIComponent(id)}/speech?q=${encodeURIComponent(query)}`);if(activeArchive!==id||$('archive-search-input').value.trim()!==query)return;
    currentSpeech=result.items;
    $('archive-speech').innerHTML=`<p class="muted">${query?'找到 '+currentSpeech.length+' 条匹配话术':'已保存约 '+(currentSpeech.reduce((sum,r)=>sum+r.end_seconds-r.start_seconds,0)/60).toFixed(1)+' 分钟话术'}。点击时间定位录像；未录视频时可听对应录音。</p><div class="archive-transcripts">${currentSpeech.map(r=>`<article><button class="speech-time text-button" type="button" data-play-speech="${r.id}">${e(L.fullTime(r.captured_at))}<small>${r.video?'定位录像':'听分段录音'}</small></button><p>${query?e(r.text).split(e(query)).join('<mark>'+e(query)+'</mark>'):e(r.text||({pending:'等待识别',processing:'正在识别',silent:'未检测到人声',error:'识别失败'})[r.status]||'')}</p></article>`).join('')}</div>`;
  }
  function switchArchiveTab(name){
    document.querySelectorAll('[data-archive-panel]').forEach(panel=>panel.hidden=panel.dataset.archivePanel!==name);
    document.querySelectorAll('[data-archive-tab]').forEach(button=>{button.classList.toggle('active',button.dataset.archiveTab===name);button.setAttribute('aria-pressed',String(button.dataset.archiveTab===name));});
  }
  function playMedia(id,offset=0){
    $('archive-player-zone').hidden=false;
    const player=$('archive-player'),audio=$('archive-audio');audio.pause();audio.hidden=true;player.hidden=false;
    player.src=`/api/media/${encodeURIComponent(id)}`;
    player.addEventListener('loadedmetadata',()=>{player.currentTime=Math.max(0,Math.min(offset,Math.max(0,player.duration-.1)));player.play().catch(()=>{});},{once:true});
    $('archive-play-note').textContent='已按采音时间定位。直播缓存可能造成时间偏差，可用进度条微调。';player.scrollIntoView({block:'center'});
  }
  $('archive-detail').addEventListener('click',async event=>{
    const location=event.target.closest('[data-open-media-folder]');if(location){await L.openRecordingLocation(`/api/media/${encodeURIComponent(location.dataset.openMediaFolder)}/open-folder`,location);return;}
    const media=event.target.closest('[data-play-media]');if(media){playMedia(media.dataset.playMedia);return;}
    const speech=event.target.closest('[data-play-speech]');if(speech){
      const row=currentSpeech.find(r=>r.id===Number(speech.dataset.playSpeech));if(!row)return;
      if(row.video)playMedia(row.video.id,row.video.offset);else{
        $('archive-player-zone').hidden=false;$('archive-player').pause();$('archive-player').hidden=true;const audio=$('archive-audio');audio.hidden=false;audio.src=`/api/speech/${row.id}/audio`;
        audio.addEventListener('loadedmetadata',()=>{audio.currentTime=row.match_offset||0;audio.play().catch(()=>{});},{once:true});
        $('archive-play-note').textContent='此处没有可播放的对应录像，已打开当时的分段录音。';audio.scrollIntoView({block:'center'});
      }return;
    }
    const retry=event.target.closest('[data-retry-media]');if(retry){try{await L.request(`/api/media/${encodeURIComponent(retry.dataset.retryMedia)}/retry`,{method:'POST',body:{}});L.toast('已加入 MP4 生成队列。');await showArchive(activeArchive);}catch(error){L.toast(error.message,true);}}
  });
  $('archive-dialog').addEventListener('close',()=>{const video=$('archive-player'),audio=$('archive-audio');if(video){video.pause();video.removeAttribute('src');video.load();}if(audio){audio.pause();audio.removeAttribute('src');audio.load();}});
  function renderAnalysis(result){
    $('archive-analysis').innerHTML=`<h4>直播结构拆解</h4><p class="room-warning">${e(result.method)}：${e(result.notice)}</p><div class="archive-stats">${Object.entries(result.seconds_by_category).map(([name,seconds])=>`<span>${e(name)}<strong>${(seconds/60).toFixed(1)} 分钟</strong></span>`).join('')}</div><h4>按时间查看结构与原话</h4><div class="analysis-timeline">${result.timeline.map(t=>`<article><div><strong>${e(t.category)}</strong><button class="text-button" type="button" data-play-speech="${t.speech_id}">${e(L.fullTime(t.captured_at))}</button></div><small>识别依据：${e(t.evidence_terms.join('、')||'证据不足，待人工判断')}${t.labels.length>1?' · 同时涉及 '+e(t.labels.join('、')):''}</small><p>${e(t.quote)}</p></article>`).join('')}</div><h4>重复话术</h4>${result.repeated_passages.map(r=>`<p>${r.occurrences.length} 次：${e(r.text)}</p>`).join('')||'<p class="muted">未找到跨片段重复的完整句子。</p>'}<h4>转化轮次候选 ${result.conversion_round_candidates.length} 处</h4>${result.conversion_round_candidates.map(r=>`<p><button type="button" class="text-button" data-play-speech="${r.speech_id}">${e(L.fullTime(r.captured_at))}</button> ${e(r.quote)}</p>`).join('')}<p class="muted">${e(result.limitations.join(' '))}</p>`;
    // Analysis references all speech, even if a search had narrowed the list.
    const requested=activeArchive;L.request(`/api/archives/${encodeURIComponent(requested)}/speech`).then(r=>{if(activeArchive===requested)currentSpeech=r.items;}).catch(()=>{});
  }

  async function loadPush(){
    const data=await L.request('/api/push');$('push-enabled').checked=data.enabled;$('push-webhook').value='';$('push-secret').value='';$('push-clear-secret').checked=false;
    $('push-webhook').placeholder=data.configured?'已保存；留空保持原地址':'https://open.feishu.cn/open-apis/bot/v2/hook/…';
    $('push-secret').placeholder=data.has_secret?'已保存签名密钥；留空保持':'未设置签名密钥';
    $('push-events').innerHTML=Object.entries(data.labels).map(([key,label])=>`<label class="checkbox-label"><input type="checkbox" name="push-event" value="${e(key)}" ${data.events.includes(key)?'checked':''}>${e(label)}</label>`).join('');
    $('push-logs').innerHTML='<h4>最近推送记录</h4>'+data.logs.map(r=>`<p>${e(data.labels[r.kind]||r.kind||'')}：${e(({pending:'等待重试',sending:'发送中',sent:'已发送',failed:'失败',cancelled:'设置变更后取消',expired:'已过期'})[r.status]||r.status)}${r.error?' · '+e(r.error):''}</p>`).join('');
  }
  $('push-button').addEventListener('click',async()=>{try{await loadPush();$('push-message').textContent='';$('push-dialog').showModal();}catch(error){L.toast(error.message,true);}});
  $('push-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try{await L.request('/api/push',{method:'POST',body:{enabled:$('push-enabled').checked,webhook:$('push-webhook').value.trim(),secret:$('push-secret').value.trim(),clear_secret:$('push-clear-secret').checked,events:[...document.querySelectorAll('[name="push-event"]:checked')].map(x=>x.value)}});await loadPush();$('push-message').textContent='设置已保存。开启后仅发送之后发生的新事件。';}
    catch(error){$('push-message').textContent=error.message;}finally{button.disabled=false;}
  });
  $('push-test').addEventListener('click',async()=>{
    const button=$('push-test');button.disabled=true;
    try{await L.request('/api/push/test',{method:'POST',body:{}});$('push-message').textContent='飞书已接受测试消息，请在群内查看。';}catch(error){$('push-message').textContent=error.message;}finally{button.disabled=false;}
  });
})();
