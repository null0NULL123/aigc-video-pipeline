const{createApp,ref,reactive,computed,onMounted}=Vue
const app=createApp({
  setup(){
    const activeTab=ref('generate')
    const isNarrow=ref(window.matchMedia('(max-width:900px)').matches)
    window.matchMedia('(max-width:900px)').addEventListener('change',e=>isNarrow.value=e.matches)
    const menuDlg=ref(false)
    const menuItems=[
      {index:'generate',label:'视频生成',icon:ElementPlusIconsVue.Lightning},
      {index:'images',label:'图片生成',icon:ElementPlusIconsVue.Picture},
      {index:'library',label:'素材库',icon:ElementPlusIconsVue.Film},
      {index:'merge',label:'合并',icon:ElementPlusIconsVue.CopyDocument},
      {index:'settings',label:'设置',icon:ElementPlusIconsVue.Setting},
    ]
    const errMsg=e=>e?.response?.data?.detail||e?.message||'请求失败'
    const toastError=(e,prefix='操作失败')=>ElementPlus.ElMessage.error(`${prefix}：${errMsg(e)}`)
    const onUploadError=()=>ElementPlus.ElMessage.error('上传失败，请重试')

    // ── 表格 ──
    const tables=ref([]),curTable=ref('')
    const shotDlg=ref(false),shotSaving=ref(false),shotForm=ref({})
    const tableListOpen=ref(false)
    const workflowOpts=ref([])
    const loadWorkflows=async()=>{
      try{const{data}=await axios.get('/api/templates');workflowOpts.value=data}catch(e){toastError(e,'加载工作流失败')}
    }
    const primaryAssetType=row=>{
      const ff=row?.first_frame||''
      const assets=row?.assets||[]
      if(ff || assets.some(a=>a.type==='image'&&a.path))return 'image'
      return 'ai_generated'
    }
    const wfOptionsFor=row=>{
      const at=primaryAssetType(row)
      return workflowOpts.value.filter(w=>{
        const list=w.match_rules?.asset_type||[]
        if(at==='image'||at==='local')return list.includes('image')||list.includes('local')
        return list.includes('ai_generated')||list.includes('none')||list.includes('')
      })
    }
    const assetCount=(row,t)=>row?.assets?.filter(a=>a.type===t&&a.path).length||0
    const allImages=computed(()=>{
      // /api/videos 返回的 path 已相对 output/；不要再拼 output/。
      const gen=videosList.value.filter(v=>v.type==='image').map(v=>({name:v.filename,path:v.path}))
      const as=assetImages.value.map(a=>({name:a.name,path:a.path}))
      return [...gen,...as]
    })
    const imgResults=computed(()=>videosList.value.filter(v=>v.type==='image').sort((a,b)=>b.modified-a.modified).slice(0,20))
    const imgSrc=p=>{
      if(!p)return ''
      // 素材库路径由 assets API 服务；其它生成图片由 videos API 服务。
      const rel=p.replace(/^output\//,'')
      return (rel.startsWith('assets/')||p.startsWith('output/assets'))?('/api/assets/images/'+rel):('/api/videos/'+rel)
    }
    const patchWorkflow=async row=>{
      try{await axios.put(`/api/tables/${curTable.value}/shots/${row.id}`,{...row})}
      catch(e){toastError(e,'保存工作流失败');loadGenShots()}
    }
    const loadTables=async()=>{
      try{
        const{data}=await axios.get('/api/tables')
        tables.value=data
        if(!curTable.value||!data.find(t=>t.id===curTable.value))curTable.value=data[0]?.id||''
      }catch(e){toastError(e,'加载表格失败')}
    }
    const selectTable=id=>{curTable.value=id;tableListOpen.value=false}
    const createTable=async()=>{
      let name=''
      try{name=(await ElementPlus.ElMessageBox.prompt('表格名称','新建表格',{inputValue:'新表格'})).value}catch{return}
      try{await axios.post('/api/tables',{name});loadTables();loadGenShots();ElementPlus.ElMessage.success('已创建')}catch(e){ElementPlus.ElMessage.error(e.response?.data?.detail||'创建失败')}
    }
    const deleteTable=async id=>{
      try{await axios.delete(`/api/tables/${id}`);delete genSelected[id];delete genShots[id];curTable.value='';loadTables();loadGenShots();ElementPlus.ElMessage.success('已删除')}catch{ElementPlus.ElMessage.error('删除失败')}
    }
    const assetImages=ref([])
    const loadAssets=async()=>{try{const{data}=await axios.get('/api/assets/images');assetImages.value=data.images}catch(e){toastError(e,'加载素材失败')}}
    const onAssetUploaded=res=>{ElementPlus.ElMessage.success(res.message||'上传成功');loadAssets()}
    const openShotDialog=row=>{shotForm.value=row?{...row}:{duration:4,scene_desc:'',dialogue:'',screen_text:'',asset_type:'ai_generated',asset_path:'',assets:[],first_frame:'',last_frame:'',workflow_id:''};loadAssets();loadVideos();shotDlg.value=true}
    const saveShot=async()=>{
      shotSaving.value=true
      try{
        if(shotForm.value.id)await axios.put(`/api/tables/${curTable.value}/shots/${shotForm.value.id}`,shotForm.value)
        else await axios.post(`/api/tables/${curTable.value}/shots`,shotForm.value)
        shotDlg.value=false;loadGenShots();loadTables();ElementPlus.ElMessage.success('保存成功')
      }catch(e){ElementPlus.ElMessage.error('失败: '+(e.response?.data?.detail||e.message))}
      shotSaving.value=false
    }
    const deleteShot=async id=>{try{await axios.delete(`/api/tables/${curTable.value}/shots/${id}`);loadGenShots();loadTables();ElementPlus.ElMessage.success('已删除')}catch{ElementPlus.ElMessage.error('删除失败')}}
    const onImportSuccess=res=>{ElementPlus.ElMessage.success(res.message||'导入成功');loadGenShots();loadTables()}
    const exportShots=()=>window.open(`/api/tables/${curTable.value}/export`,'_blank')
    const moveShot=async(index,dir)=>{
      const shots=[...(genShots[curTable.value]||[])]
      const target=index+dir
      ;[shots[index],shots[target]]=[shots[target],shots[index]]
      genShots[curTable.value]=shots
      try{await axios.put(`/api/tables/${curTable.value}/shots/reorder`,{ordered_ids:shots.map(s=>s.id)})}catch{loadGenShots()}
    }

    // ── 生成 ──
    const genShots=reactive({}),genSelected=reactive({}),genName=ref('')
    const loadGenShots=async()=>{
      await loadTables()
      for(const t of tables.value){
        try{const{data}=await axios.get(`/api/tables/${t.id}/shots`);genShots[t.id]=data}catch(e){toastError(e,'加载镜头失败')}
      }
    }
    const toggleGen=(tid,sid,val)=>{
      const cur=genSelected[tid]||[]
      if(val){if(!cur.includes(sid))genSelected[tid]=[...cur,sid]}
      else genSelected[tid]=cur.filter(x=>x!==sid)
    }
    const selectAllInTable=(tid,on)=>{
      genSelected[tid]=on?(genShots[tid]||[]).map(s=>s.id):[]
    }
    const genTotal=computed(()=>Object.values(genSelected).reduce((a,b)=>a+(b?.length||0),0))

    const pipelineRunning=ref(false),pipelineStatus=ref({}),pipelineLogs=ref(''),logsLoading=ref(false)
    const imgPrompt=ref(''),imgCount=ref(1)
    const runImageGen=async()=>{
      if(!imgPrompt.value.trim()){ElementPlus.ElMessage.warning('请输入画面描述');return}
      pipelineRunning.value=true
      try{
        await axios.post('/api/images/generate',{prompt:imgPrompt.value,count:imgCount.value})
        ElementPlus.ElMessage.success('已开始生成')
        pollStatus()
      }catch(e){ElementPlus.ElMessage.error(e.response?.data?.detail||'启动失败');pipelineRunning.value=false}
    }
    const downloadImg=path=>window.open('/api/videos/'+path,'_blank')
    const genFrame=async field=>{
      let desc=''
      try{desc=(await ElementPlus.ElMessageBox.prompt('描述画面（文生图，将自动填入）','生成'+(field==='first_frame'?'首帧':'尾帧'),{inputValue:''})).value?.trim()}catch{return}
      if(!desc)return
      try{await axios.post('/api/images/generate',{prompt:desc,count:1})}
      catch(e){toastError(e,'启动失败');return}
      ElementPlus.ElMessage.success('已在后台生成，完成后自动填入')
      const t0=Date.now()
      const timer=setInterval(async()=>{
        if(Date.now()-t0>180000){clearInterval(timer);return}
        try{
          await loadVideos()
          const gens=videosList.value.filter(v=>v.type==='image').sort((a,b)=>b.modified-a.modified)
          const fresh=gens[0]
          if(fresh && fresh.modified>=(t0/1000)-2){
            shotForm.value[field]='output/'+fresh.path
            ElementPlus.ElMessage.success('已自动填入')
            clearInterval(timer)
          }
        }catch(e){}
      },3000)
    }
    const runGenerate=async()=>{
      const selections=Object.keys(genSelected).filter(tid=>genSelected[tid]?.length).map(tid=>({table_id:tid,shot_ids:genSelected[tid]}))
      if(!selections.length){ElementPlus.ElMessage.warning('请先选择镜头');return}
      pipelineRunning.value=true
      try{
        await axios.post('/api/generate',{name:genName.value||undefined,selections})
        pollStatus()
      }catch(e){ElementPlus.ElMessage.error(e.response?.data?.detail||'启动失败');pipelineRunning.value=false}
    }
    const pollStatus=async()=>{
      try{
        const{data}=await axios.get('/api/pipeline/status')
        pipelineStatus.value=data
        if(data.running){refreshLogs();setTimeout(pollStatus,3000)}
        else{
          pipelineRunning.value=false
          refreshLogs()
          loadTables();loadGenShots();loadVideos()
        }
      }catch{pipelineRunning.value=false}
    }
    let logTotal=0
    const refreshLogs=async()=>{
      logsLoading.value=true
      try{
        const{data}=await axios.get('/api/pipeline/logs',{params:{tail:200}})
        if(data.total<logTotal){pipelineLogs.value='';logTotal=0}
        if(data.total>logTotal){
          const add=data.lines.slice(Math.max(0,data.lines.length-(data.total-logTotal)))
          pipelineLogs.value=(pipelineLogs.value?pipelineLogs.value+'\n':'')+add.join('\n')
          logTotal=data.total
        }
      }catch(e){toastError(e,'加载日志失败')}
      logsLoading.value=false
    }

    // ── 素材库 ──
    const videosList=ref([]),videoBatches=ref({}),hiddenVideos=ref([])
    const expandedGroups=reactive({}),navActive=ref('')
    // API 返回的 path 已相对 output/；合并、配音和 /api/videos 都使用这个相对路径。
    const mediaVideos=computed(()=>videosList.value.filter(v=>v.type==='video').map(v=>({...v,name:v.filename})))
    const loadVideos=async()=>{
      try{
        const{data}=await axios.get('/api/videos',{params:{include_hidden:1}})
        videosList.value=data.videos;videoBatches.value=data.batches;hiddenVideos.value=data.hidden||[]
        loadAssets()
      }catch(e){toastError(e,'加载素材库失败')}
    }
    const onVideoImported=res=>{ElementPlus.ElMessage.success(res.message||'导入成功');loadVideos()}
    // 顶级类型组：视频素材 / 图片素材；子级是各 batch + 素材图片 + 已隐藏
    const navGroups=computed(()=>{
      const videoChildren=[]
      const imageChildren=[]
      for(const[batchId,items]of Object.entries(videoBatches.value)){
        const vCount=items.filter(x=>x.type==='video').length
        const iCount=items.filter(x=>x.type==='image').length
        if(vCount>0)videoChildren.push({id:'b:'+batchId,name:batchId,count:vCount})
        if(iCount>0)imageChildren.push({id:'b:'+batchId,name:batchId,count:iCount})
      }
      if(assetImages.value.length)imageChildren.push({id:'g:__assets__',name:'素材图片',count:assetImages.value.length})
      if(hiddenVideos.value.length)videoChildren.push({id:'g:__hidden__',name:'已隐藏',count:hiddenVideos.value.length})
      const sum=a=>a.reduce((t,c)=>t+c.count,0)
      return[
        {id:'type:video',name:'视频素材',total:sum(videoChildren),children:videoChildren},
        {id:'type:image',name:'图片素材',total:sum(imageChildren),children:imageChildren},
      ]
    })
    const batchCount=batch=>{
      const v=batch.filter(x=>x.type==='video').length
      const i=batch.filter(x=>x.type==='image').length
      return `${v} 视频 · ${i} 图片`
    }
    const toggleGroup=id=>{expandedGroups[id]=expandedGroups[id]===undefined?false:!expandedGroups[id]}
    const allGroupIds=()=>{
      const ids=[]
      for(const g of navGroups.value){
        ids.push(g.id)
        for(const c of g.children)ids.push(c.id)
      }
      return ids
    }
    const expandAllGroups=()=>{allGroupIds().forEach(id=>expandedGroups[id]=true)}
    const collapseAllGroups=()=>{allGroupIds().forEach(id=>expandedGroups[id]=false)}
    // id 形如 b:batchId / g:__assets__；DOM id 不含前缀，所以 navActive 保留全名（用于高亮），DOM 查 lib- 时去前缀
    const scrollToGroup=id=>{
      navActive.value=id
      const domId=id.replace(/^b:|^g:/,'')
      document.getElementById('lib-'+domId)?.scrollIntoView({behavior:'smooth',block:'start'})
    }
    const hideVideo=async path=>{
      try{await ElementPlus.ElMessageBox.confirm('隐藏后该素材不再显示（文件保留，可随时恢复）','确认隐藏',{type:'warning'})}catch{return}
      try{const{data}=await axios.post('/api/videos/hide',{path});ElementPlus.ElMessage.success(data.message);loadVideos()}catch(e){ElementPlus.ElMessage.error(e.response?.data?.detail||'隐藏失败')}
    }
    const unhideVideo=async path=>{
      try{const{data}=await axios.post('/api/videos/unhide',{path});ElementPlus.ElMessage.success(data.message);loadVideos()}catch(e){ElementPlus.ElMessage.error(e.response?.data?.detail||'恢复失败')}
    }
    const deleteVideo=async path=>{
      try{await ElementPlus.ElMessageBox.confirm('确认物理删除该视频？（仅导入视频，不可恢复）','确认删除',{type:'warning'})}catch{return}
      try{await axios.delete('/api/videos',{params:{path}});loadVideos();ElementPlus.ElMessage.success('已删除')}catch(e){ElementPlus.ElMessage.error(e.response?.data?.detail||'删除失败')}
    }
    const deleteAssetImage=async path=>{
      try{await ElementPlus.ElMessageBox.confirm('确认物理删除该图片素材？（不可恢复）','确认删除',{type:'warning'})}catch{return}
      try{const{data}=await axios.delete('/api/assets/images',{params:{path}});ElementPlus.ElMessage.success(data.message);loadAssets()}catch(e){ElementPlus.ElMessage.error(e.response?.data?.detail||'删除失败')}
    }
    const downloadBatch=id=>window.open(`/api/batches/${id}/download`,'_blank')

    // ── 合并 ──
    const mergeSel=ref([]),mergeMode=ref('concat'),mergeDur=ref(0.5),mergeName=ref('')
    const mergeRunning=ref(false),mergeResult=ref('')
    const previewDlg=ref(false),previewVideo=ref(null)
    const openPreview=v=>{previewVideo.value=v;previewDlg.value=true}
    const isMergeSel=path=>mergeSel.value.some(v=>v.path===path)
    const toggleMerge=v=>{
      const i=mergeSel.value.findIndex(x=>x.path===v.path)
      if(i>=0)mergeSel.value.splice(i,1)
      else mergeSel.value.push({...v})
    }
    const mergeMove=(i,dir)=>{
      const j=i+dir
      const arr=mergeSel.value
      ;[arr[i],arr[j]]=[arr[j],arr[i]]
    }
    const mergeRemove=i=>mergeSel.value.splice(i,1)
    const runMerge=async()=>{
      mergeRunning.value=true;mergeResult.value=''
      try{
        const{data}=await axios.post('/api/merge',{
          video_paths:mergeSel.value.map(v=>v.path),
          name:mergeName.value||undefined,
          mode:mergeMode.value,
          transition_duration:mergeDur.value,
        })
        mergeResult.value=data.path
        ElementPlus.ElMessage.success(data.message)
        loadVideos()
      }catch(e){ElementPlus.ElMessage.error(e.response?.data?.detail||e.message)}
      mergeRunning.value=false
    }

    // ── 设置 ──
    const cfg=ref({}),cfgLoading=ref(true),cfgSaving=ref(false),cfgResetting=ref(false)
    const templates=ref([]),openPanels=ref(['models','media','paths','agent','templates'])
    const kwMapText=ref(''),saveResultDlg=ref(false),saveResult=ref({})
    const tplDlg=ref(false),tplDlgContent=ref('')
    const yamlize=obj=>Object.entries(obj||{}).map(([k,v])=>`${k}: ${v}`).join('\n')
    const parseYaml=text=>{const r={};text.split('\n').forEach(l=>{l=l.trim();if(!l||l.startsWith('#'))return;const i=l.indexOf(':');if(i>0)r[l.slice(0,i).trim()]=l.slice(i+1).trim()});return r}
    const loadCfg=async()=>{cfgLoading.value=true;try{const{data}=await axios.get('/api/config');cfg.value=data;
      cfg.value.llm=cfg.value.llm||{};cfg.value.ffmpeg=cfg.value.ffmpeg||{};cfg.value.merge=cfg.value.merge||{};
      if(cfg.value.ffmpeg.subtitle===undefined)cfg.value.ffmpeg.subtitle={}
      if(cfg.value.merge.tts_mode===undefined)cfg.value.merge.tts_mode='whole'
      if(cfg.value.llm.enabled===undefined)cfg.value.llm.enabled=true
      kwMapText.value=yamlize(data.agent?.keyword_map)}catch(e){toastError(e,'加载配置失败')};cfgLoading.value=false}
    const loadTpls=async()=>{try{const{data}=await axios.get('/api/templates');templates.value=data}catch(e){toastError(e,'加载模板失败')}}
    const saveCfg=async()=>{cfgSaving.value=true;try{cfg.value.agent.keyword_map=parseYaml(kwMapText.value);const{data}=await axios.put('/api/config',cfg.value);saveResult.value=data;saveResultDlg.value=true}catch{saveResult.value={ok:false,message:'保存失败'};saveResultDlg.value=true};cfgSaving.value=false}
    const resetCfg=async()=>{try{await ElementPlus.ElMessageBox.confirm('确定从备份恢复？','确认',{type:'warning'})}catch{return};cfgResetting.value=true;try{const{data}=await axios.post('/api/config/reset');cfg.value=data.config;kwMapText.value=yamlize(data.config.agent?.keyword_map);ElementPlus.ElMessage.success(data.message)}catch{ElementPlus.ElMessage.error('恢复失败')};cfgResetting.value=false}
    const viewTpl=async id=>{try{const{data}=await axios.get(`/api/templates/${id}`);tplDlgContent.value=JSON.stringify(data,null,2);tplDlg.value=true}catch(e){toastError(e,'加载模板失败')}}
    const onTplImported=res=>{ElementPlus.ElMessage.success(res.message||'导入成功');loadTpls()}

    // ── 顶栏服务状态 ──
    const comfyOnline=ref(false)
    const loadSystem=async()=>{
      try{const{data}=await axios.get('/api/system/status');comfyOnline.value=data.comfyui?.online||false}catch{comfyOnline.value=false}
    }

    // ═══════════════════════════════════════════════════════
    // 镜头 Review & 重做（SSE 实时进度）
    // ═══════════════════════════════════════════════════════
    const reviewBatches=ref([])      // [{batch_id, started_at, total, done, failed, confirmed}]
    const currentBatch=ref(null)     // 当前查看的 batch_id
    const batchShots=ref([])         // 当前 batch 的所有 shot 状态
    const shotsLoading=ref(false)
    const confirmLoading=ref({})     // {shot_key: bool} 防止重复点击
    let evtSource=null

    // 加载批次列表
    const loadBatches=async()=>{
      try{const{data}=await axios.get('/api/pipeline/batches');reviewBatches.value=data.batches||[]}
      catch(e){toastError(e,'加载批次列表失败')}
    }

    // 加载某个 batch 的 shots
    const loadBatchShots=async(batchId)=>{
      currentBatch.value=batchId
      shotsLoading.value=true
      try{const{data}=await axios.get(`/api/pipeline/batches/${batchId}/shots`);batchShots.value=data.shots||[]}
      catch(e){toastError(e,'加载镜头失败')}
      shotsLoading.value=false
    }

    // 确认/取消确认
    const confirmShot=async(shotKey)=>{
      if(confirmLoading.value[shotKey])return
      confirmLoading.value[shotKey]=true
      try{
        const shot=batchShots.value.find(s=>s.key===shotKey)
        if(!shot)return
        if(shot.status==='done'){
          await axios.post(`/api/pipeline/batches/${currentBatch.value}/shots/${shotKey}/confirm`)
          shot.status='confirmed'
        }else if(shot.status==='confirmed'){
          await axios.post(`/api/pipeline/batches/${currentBatch.value}/shots/${shotKey}/unconfirm`)
          shot.status='done'
        }
      }catch(e){toastError(e,'操作失败')}
      confirmLoading.value[shotKey]=false
    }

    // 批量确认所有 done 的镜头
    const confirmAllDone=async()=>{
      const doneShots=batchShots.value.filter(s=>s.status==='done')
      for(const s of doneShots)await confirmShot(s.key)
    }

    // 重做镜头
    const redoShot=async(shotKey)=>{
      if(confirmLoading.value[shotKey])return
      confirmLoading.value[shotKey]=true
      try{
        await axios.post(`/api/pipeline/batches/${currentBatch.value}/shots/${shotKey}/redo`)
        // shot 状态重置为 pending，SSE 会更新
      }catch(e){toastError(e,'重做失败')}
      confirmLoading.value[shotKey]=false
    }

    // 重做所有失败镜头
    const redoFailed=async()=>{
      const failed=batchShots.value.filter(s=>s.status==='failed')
      for(const s of failed)await redoShot(s.key)
    }

    // 合并已确认镜头（复用上面的 mergeRunning ref）
    const mergeConfirmed=async()=>{
      mergeRunning.value=true
      try{
        const{data}=await axios.post(`/api/pipeline/batches/${currentBatch.value}/merge`)
        ElementPlus.ElMessage.success(`合并已启动：${data.message}`)
        pollStatus() // 刷新 pipeline 状态
      }catch(e){toastError(e,'合并失败')}
      mergeRunning.value=false
    }

    // SSE 订阅当前 batch
    const subscribeSSE=(batchId)=>{
      if(evtSource){evtSource.close();evtSource=null}
      evtSource=new EventSource(`/api/pipeline/batches/${batchId}/events`)
      evtSource.addEventListener('shot_update',e=>{
        try{
          const d=JSON.parse(e.data)
          const key=d.shot_key
          const shot=batchShots.value.find(s=>s.key===key)
          if(shot && d.fields){
            Object.assign(shot,d.fields)
          }
        }catch{}
      })
      evtSource.addEventListener('batch_done',e=>{
        // batch 结束，关闭 SSE，刷新列表
        if(evtSource){evtSource.close();evtSource=null}
        loadBatches()
      })
      evtSource.onerror=()=>{
        // 断线自动重连（简单重试）
        setTimeout(()=>subscribeSSE(batchId),3000)
      }
    }

    // 打开 review 面板时订阅
    const openReviewPanel=async(batchId)=>{
      await loadBatchShots(batchId)
      subscribeSSE(batchId)
    }

    // 关闭时断开
    const closeReviewPanel=()=>{
      if(evtSource){evtSource.close();evtSource=null}
      currentBatch.value=null
      batchShots.value=[]
    }

    const switchTab=tab=>{
      activeTab.value=tab
      if(tab==='generate'){loadTables();loadGenShots();pollStatus()}
      if(tab==='images'){loadVideos()}
      if(tab==='library')loadVideos()
      if(tab==='merge')loadVideos()
      if(tab==='settings'){loadCfg();loadTpls()}
    }

    onMounted(()=>{loadTables();loadGenShots();loadWorkflows();loadSystem();loadBatches()})

    return{
      activeTab,switchTab,isNarrow,menuDlg,menuItems,onUploadError,
      tables,curTable,shotDlg,shotSaving,shotForm,tableListOpen,
      workflowOpts,wfOptionsFor,patchWorkflow,
      loadTables,selectTable,createTable,deleteTable,openShotDialog,saveShot,deleteShot,onImportSuccess,exportShots,moveShot,
      assetImages,loadAssets,onAssetUploaded,
      imgPrompt,imgCount,imgResults,runImageGen,downloadImg,genFrame,imgSrc,assetCount,allImages,
      genShots,genSelected,genName,genTotal,loadGenShots,toggleGen,selectAllInTable,
      pipelineRunning,pipelineStatus,pipelineLogs,logsLoading,runGenerate,refreshLogs,
      videosList,videoBatches,hiddenVideos,mediaVideos,loadVideos,onVideoImported,deleteVideo,downloadBatch,
      navGroups,batchCount,toggleGroup,expandAllGroups,collapseAllGroups,scrollToGroup,navActive,expandedGroups,
      hideVideo,unhideVideo,deleteAssetImage,
      mergeSel,mergeMode,mergeDur,mergeName,mergeRunning,mergeResult,isMergeSel,toggleMerge,mergeMove,mergeRemove,runMerge,
      previewDlg,previewVideo,openPreview,
      cfg,cfgLoading,cfgSaving,cfgResetting,templates,openPanels,kwMapText,saveResultDlg,saveResult,tplDlg,tplDlgContent,loadCfg,saveCfg,resetCfg,viewTpl,onTplImported,
      comfyOnline,
      // Review & 重做
      reviewBatches,currentBatch,batchShots,shotsLoading,confirmLoading,
      loadBatches,loadBatchShots,confirmShot,confirmAllDone,redoShot,redoFailed,mergeConfirmed,mergeRunning,
      openReviewPanel,closeReviewPanel,
    }
  }
})
app.use(ElementPlus)
for(const[k,c]of Object.entries(ElementPlusIconsVue))app.component(k,c)
app.mount('#app')
