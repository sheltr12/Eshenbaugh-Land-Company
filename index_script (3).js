
const PAGE_SIZE = 500;
const DISPLAY_COLUMNS = [
  ['_select','Select'],['num','#'],['property_type','Property Type'],['address','Address'],['sale_date','Sale Date'],
  ['price','Price'],['acreage','Acres'],['price_per_acre','$/Acre'],['unit_count','Units'],['price_per_unit','$/Unit'],['elc_deal','ELC Deal'],['grantor','Seller'],
  ['grantee','Buyer'],['zoning','Zoning'],['land_use','Land Use'],['source','Source'],['aerial','Aerial'],['_actions','Actions']
];
const FIELD_LABELS = {
  num:'#',parcel_id:'Parcel ID',parcel_raw_id:'Parcel Raw ID',address:'Address',sale_date:'Sale Date',
  price:'Price',acreage:'Acres',price_per_acre:'$/Acre',book_page:'Book/Page',transaction_type:'Transaction Type',
  grantor:'Seller',grantee:'Buyer',deed_type:'Deed Type',property_type:'Property Type',zoning:'Zoning',
  land_use:'Land Use',source:'Source',comments:'Comments',lat:'Latitude',lon:'Longitude',
  coords_approx:'Coordinates Approximate',deed_url:'Deed URL',aerial:'Aerial',sale_date_iso:'Sale Date ISO',unit_count:'Unit Count',price_per_unit:'$/Unit',elc_deal:'ELC Deal'
};
const currencyFields = new Set(['price','price_per_acre','price_per_unit']);
const numericFields = new Set(['price','price_per_acre','price_per_unit','acreage','unit_count','lat','lon','num']);
const escapeHtml = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const hasValue = v => v !== null && v !== undefined && v !== '';
const AERIAL_FIELDS = ['aerial','photo_url','photoUrl','photoURL','Photo URL','PHOTO URL','photo url','photo','Photo','image_url','imageUrl','image','aerial_url','aerialUrl','Aerial URL','AERIAL URL','Aerial'];
function getAerialValue(c){
  for(const field of AERIAL_FIELDS){
    if(c && hasValue(c[field])) return c[field];
  }
  return null;
}
function normalizeComp(c){
  const aerial = getAerialValue(c);
  return aerial && !hasValue(c.aerial) ? {...c, aerial} : c;
}
function compMatchKeys(c){
  const keys=[];
  if(hasValue(c.num)) keys.push('num:' + String(c.num));
  if(hasValue(c.address)) keys.push('addr:' + String(c.address).trim().toLowerCase() + '|date:' + String(c.sale_date||c.sale_date_iso||'').trim().toLowerCase() + '|price:' + String(c.price||''));
  return keys;
}
function mergeAerialsFromBackup(comps, backupComps){
  const aerialByKey = new Map();
  (backupComps||[]).map(normalizeComp).forEach(b=>{
    const aerial = getAerialValue(b);
    if(!hasValue(aerial)) return;
    compMatchKeys(b).forEach(k=>{ if(!aerialByKey.has(k)) aerialByKey.set(k, aerial); });
  });
  return (comps||[]).map(c=>{
    let out = normalizeComp(c);
    if(hasValue(out.aerial)) return out;
    for(const k of compMatchKeys(out)){
      if(aerialByKey.has(k)) return {...out, aerial:aerialByKey.get(k)};
    }
    return out;
  });
}
async function loadAerialBackup(){
  for(const file of ['./comps_aerial_backup.json','./comps_updated.json','./comps_with_aerials.json']){
    try{
      const res = await fetch(file, {cache:'no-store'});
      if(res.ok) return await res.json();
    }catch(e){}
  }
  return null;
}
const fmtMoney = n => hasValue(n) ? '$' + Math.round(Number(n)).toLocaleString() : '<span class="null-val">—</span>';
const fmtAcres = n => hasValue(n) ? Number(n).toLocaleString(undefined,{maximumFractionDigits:2}) : '<span class="null-val">—</span>';
const fmtStr = s => hasValue(s) ? escapeHtml(s) : '<span class="null-val">—</span>';
const fmtLink = (url,label='Aerial') => hasValue(url) ? `<a class="aerial-btn" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label}</a>` : '<span class="null-val">—</span>';
const parseCompDate = c => {
  const raw = c.sale_date_iso || c.sale_date;
  if(!hasValue(raw)) return null;
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
};
let allComps=[], filteredComps=[], currentPage=1, sortKey='num', sortDir='asc', baseMeta={};
const LOCAL_DEALS_KEY='elc_manual_added_deals_v1';
const LOCAL_EDITS_KEY='elc_comp_edits_v1';
let selectedCompKeys = new Set();
let editingCompKey = null;

function getCompKey(c, fallbackIndex){
  if(hasValue(c._compKey)) return c._compKey;
  if(hasValue(c._manual_id)) return 'manual:' + String(c._manual_id);
  if(hasValue(c.num)) return 'num:' + String(c.num);
  if(hasValue(c.address)) return 'addr:' + String(c.address).trim().toLowerCase() + '|date:' + String(c.sale_date || c.sale_date_iso || '').trim().toLowerCase() + '|price:' + String(c.price || '');
  return 'fallback:' + String(fallbackIndex);
}
function loadLocalEdits(){ try{return JSON.parse(localStorage.getItem(LOCAL_EDITS_KEY) || '{}');}catch(e){return {};} }
function saveLocalEdits(edits){ localStorage.setItem(LOCAL_EDITS_KEY, JSON.stringify(edits)); }
function applyLocalEdits(comps){
  const edits = loadLocalEdits();
  return (comps || []).map((c,i)=>{
    const key = getCompKey(c,i);
    const edited = edits[key] ? {...c, ...edits[key], edited_locally:true} : c;
    return deriveCompFields({...edited, _compKey:key});
  });
}
function saveCompEdit(comp){
  if(!comp || !comp._compKey) return;
  const edits = loadLocalEdits();
  edits[comp._compKey] = {...comp, _compKey:undefined};
  saveLocalEdits(edits);
}
function refreshAfterDataChange(){
  allComps = applyLocalEdits(allComps);
  populateFilters(); currentPage=1; applyFilters(); updateSelectedCount();
}

async function loadReport(){
  try{
    const data = await (await fetch('./comps.json')).json();
    const aerialBackup = await loadAerialBackup();
    baseMeta = data.meta || {};
    const savedDeals = loadLocalDeals().map(normalizeComp);
    const baseComps = mergeAerialsFromBackup(data.comps || [], aerialBackup?.comps || aerialBackup || []);
    allComps = applyLocalEdits([...baseComps, ...savedDeals]);
    const meta = baseMeta;
    document.getElementById('title').textContent = `${meta.title || 'Eshenbaugh Land Company Comps'} — ${meta.period || 'Historical'}`;
    document.getElementById('subtitle').textContent = `Source: ${meta.source || 'Eshenbaugh Land Company'} | ${(allComps.length).toLocaleString()} comps | Generated ${meta.generated || ''} | ${allComps.filter(c=>hasValue(c.aerial)).length.toLocaleString()} aerial links${savedDeals.length ? ' | ' + savedDeals.length.toLocaleString() + ' locally added' : ''}`;
    renderTableHead();
    populateFilters();
    addFilterListeners();
    applyFilters();
  }catch(e){
    document.getElementById('title').textContent = 'Error loading data';
    document.getElementById('subtitle').textContent = e.message;
  }
}
function allFields(){return [...new Set(allComps.flatMap(c=>Object.keys(c)))].sort();}
function populateSelect(id, values){
  const sel=document.getElementById(id);
  [...new Set(values.filter(hasValue))].sort((a,b)=>String(a).localeCompare(String(b))).forEach(v=>{
    const opt=document.createElement('option'); opt.value=v; opt.textContent=v; sel.appendChild(opt);
  });
}
function resetSelectOptions(id, firstLabel='All'){
  const sel=document.getElementById(id); if(!sel) return;
  sel.innerHTML = id === 'field-filter' ? '<option value="">Select field</option>' : `<option value="">${firstLabel}</option>`;
}
function populateFilters(){
  resetSelectOptions('field-filter'); resetSelectOptions('filter-type'); resetSelectOptions('filter-source'); resetSelectOptions('filter-transaction');
  allFields().forEach(f=>{
    const opt=document.createElement('option'); opt.value=f; opt.textContent=FIELD_LABELS[f] || f; document.getElementById('field-filter').appendChild(opt);
  });
  populateSelect('filter-type', allComps.map(c=>c.property_type));
  populateSelect('filter-source', allComps.map(c=>c.source));
  populateSelect('filter-transaction', allComps.map(c=>c.transaction_type));
  toggleUnitPriceFilters();
}
function addFilterListeners(){
  ['search','field-filter','field-value','sale-date-from','sale-date-to','filter-type','filter-source','filter-elc','filter-transaction','filter-aerial','min-price','max-price','min-acres','max-acres','min-ppa','max-ppa','min-units','max-units','min-ppu','max-ppu'].forEach(id=>{
    document.getElementById(id).addEventListener('input',()=>{if(id==='filter-type') toggleUnitPriceFilters(); currentPage=1;applyFilters();});
  });
}
function passesSearch(c,q){
  if(!q) return true;
  return Object.values(c).filter(hasValue).join(' ').toLowerCase().includes(q);
}
function passesFieldFilter(c,field,val){
  if(!field || !val) return true;
  const raw = c[field];
  if(!hasValue(raw)) return false;
  return String(raw).toLowerCase().includes(val);
}
function applyFilters(){
  const q=document.getElementById('search').value.trim().toLowerCase();
  const field=document.getElementById('field-filter').value;
  const fieldVal=document.getElementById('field-value').value.trim().toLowerCase();
  const type=document.getElementById('filter-type').value;
  const source=document.getElementById('filter-source').value;
  const trans=document.getElementById('filter-transaction').value;
  const elc=document.getElementById('filter-elc').value;
  const aerial=document.getElementById('filter-aerial').value;
  const dateFromVal=document.getElementById('sale-date-from').value;
  const dateToVal=document.getElementById('sale-date-to').value;
  const dateFrom=dateFromVal ? new Date(dateFromVal + 'T00:00:00') : null;
  const dateTo=dateToVal ? new Date(dateToVal + 'T23:59:59') : null;
  const minPrice=parseFloat(document.getElementById('min-price').value);
  const maxPrice=parseFloat(document.getElementById('max-price').value);
  const minAcres=parseFloat(document.getElementById('min-acres').value);
  const maxAcres=parseFloat(document.getElementById('max-acres').value);
  const minPPA=parseFloat(document.getElementById('min-ppa').value);
  const maxPPA=parseFloat(document.getElementById('max-ppa').value);
  const minUnits=parseFloat(document.getElementById('min-units').value);
  const maxUnits=parseFloat(document.getElementById('max-units').value);
  const minPPU=parseFloat(document.getElementById('min-ppu').value);
  const maxPPU=parseFloat(document.getElementById('max-ppu').value);
  filteredComps=allComps.filter(c=>{
    if(type && c.property_type!==type) return false;
    if(source && c.source!==source) return false;
    if(trans && c.transaction_type!==trans) return false;
    if(elc==='yes' && c.elc_deal!=='Yes') return false;
    if(elc==='no' && c.elc_deal==='Yes') return false;
    if(aerial==='yes' && !hasValue(c.aerial)) return false;
    if(aerial==='no' && hasValue(c.aerial)) return false;
    const compDate = parseCompDate(c);
    if(dateFrom && (!compDate || compDate < dateFrom)) return false;
    if(dateTo && (!compDate || compDate > dateTo)) return false;
    if(!Number.isNaN(minPrice) && (!hasValue(c.price) || Number(c.price)<minPrice)) return false;
    if(!Number.isNaN(maxPrice) && (!hasValue(c.price) || Number(c.price)>maxPrice)) return false;
    if(!Number.isNaN(minAcres) && (!hasValue(c.acreage) || Number(c.acreage)<minAcres)) return false;
    if(!Number.isNaN(maxAcres) && (!hasValue(c.acreage) || Number(c.acreage)>maxAcres)) return false;
    if(!Number.isNaN(minPPA) && (!hasValue(c.price_per_acre) || Number(c.price_per_acre)<minPPA)) return false;
    if(!Number.isNaN(maxPPA) && (!hasValue(c.price_per_acre) || Number(c.price_per_acre)>maxPPA)) return false;
    if(!Number.isNaN(minUnits) && (!hasValue(c.unit_count) || Number(c.unit_count)<minUnits)) return false;
    if(!Number.isNaN(maxUnits) && (!hasValue(c.unit_count) || Number(c.unit_count)>maxUnits)) return false;
    if(!Number.isNaN(minPPU) && (!hasValue(c.price_per_unit) || Number(c.price_per_unit)<minPPU)) return false;
    if(!Number.isNaN(maxPPU) && (!hasValue(c.price_per_unit) || Number(c.price_per_unit)>maxPPU)) return false;
    return passesSearch(c,q) && passesFieldFilter(c,field,fieldVal);
  });
  sortComps();
  renderStats(filteredComps);
  renderPage();
}
function sortComps(){
  filteredComps.sort((a,b)=>{
    let av=a[sortKey], bv=b[sortKey];
    if(numericFields.has(sortKey)){av=hasValue(av)?Number(av):-Infinity; bv=hasValue(bv)?Number(bv):-Infinity;}
    else {av=hasValue(av)?String(av).toLowerCase():''; bv=hasValue(bv)?String(bv).toLowerCase():'';}
    if(av<bv) return sortDir==='asc'?-1:1;
    if(av>bv) return sortDir==='asc'?1:-1;
    return 0;
  });
}
function renderTableHead(){
  document.getElementById('table-head').innerHTML = DISPLAY_COLUMNS.map(([key,label])=> key.startsWith('_') ? `<th>${label}</th>` : `<th onclick="setSort('${key}')">${label}<span id="sort-${key}"></span></th>`).join('');
}
function setSort(key){
  if(sortKey===key) sortDir = sortDir==='asc'?'desc':'asc'; else {sortKey=key; sortDir='asc';}
  document.querySelectorAll('[id^="sort-"]').forEach(e=>e.textContent='');
  const el=document.getElementById('sort-'+key); if(el) el.textContent=sortDir==='asc'?' ▲':' ▼';
  applyFilters();
}
function renderStats(comps){
  const withPrice=comps.filter(c=>hasValue(c.price)), withAcres=comps.filter(c=>hasValue(c.acreage)), withPPA=comps.filter(c=>hasValue(c.price_per_acre));
  const totalVolume=withPrice.reduce((s,c)=>s+Number(c.price),0);
  const totalAcres=withAcres.reduce((s,c)=>s+Number(c.acreage),0);
  const avgPPA=withPPA.length ? withPPA.reduce((s,c)=>s+Number(c.price_per_acre),0)/withPPA.length : 0;
  const aerialCount=comps.filter(c=>hasValue(c.aerial)).length;
  document.getElementById('stats').innerHTML = `
    <div class="stat-card"><div class="val">${comps.length.toLocaleString()}</div><div class="lbl">Filtered Comps</div></div>
    <div class="stat-card"><div class="val">$${(totalVolume/1000000).toFixed(1)}M</div><div class="lbl">Volume</div></div>
    <div class="stat-card"><div class="val">${Math.round(totalAcres).toLocaleString()}</div><div class="lbl">Total Acres</div></div>
    <div class="stat-card"><div class="val">$${Math.round(avgPPA).toLocaleString()}</div><div class="lbl">Avg $/Acre</div></div>
    <div class="stat-card"><div class="val">${aerialCount.toLocaleString()}</div><div class="lbl">Aerial Links</div></div>`;
}
function toggleUnitPriceFilters(){
  const show = document.getElementById('filter-type')?.value === 'Multifamily';
  document.querySelectorAll('.ppu-filter,.unit-filter').forEach(el=>{el.style.display = show ? '' : 'none';});
  if(!show){['min-units','max-units','min-ppu','max-ppu'].forEach(id=>{const el=document.getElementById(id); if(el) el.value='';});}
}
function fmtCell(c,key){
  if(key==='_select') return `<input type="checkbox" aria-label="Select comp" ${selectedCompKeys.has(c._compKey)?'checked':''} onchange="toggleCompSelection('${escapeHtml(c._compKey)}', this.checked)">`;
  if(key==='_actions') return `<div class="action-cell"><button type="button" onclick="openEditDealModal('${escapeHtml(c._compKey)}')">Edit</button></div>`;
  const v=c[key];
  if(key==='price' || key==='price_per_acre' || key==='price_per_unit') return fmtMoney(v);
  if(key==='acreage') return fmtAcres(v);
  if(key==='unit_count') return hasValue(v) ? Number(v).toLocaleString() : '<span class="null-val">—</span>';
  if(key==='aerial') return fmtLink(v,'Aerial');
  return fmtStr(v);
}
function buildMobileCard(c){
  const comments = hasValue(c.comments) ? `<div class="mobile-card-comments"><strong>Comments:</strong> ${escapeHtml(c.comments)}</div>` : '';
  return `<div class="mobile-comp-card">
    <div class="mobile-card-title"><input type="checkbox" ${selectedCompKeys.has(c._compKey)?'checked':''} onchange="toggleCompSelection('${escapeHtml(c._compKey)}', this.checked)"> ${fmtStr(c.address)} <button class="mini-btn btn-small" type="button" onclick="openEditDealModal('${escapeHtml(c._compKey)}')">Edit</button></div>
    <div class="mobile-card-grid">
      <div class="mobile-card-field"><span>Type</span>${fmtStr(c.property_type)}</div>
      <div class="mobile-card-field"><span>Sale Date</span>${fmtStr(c.sale_date)}</div>
      <div class="mobile-card-field"><span>Price</span>${fmtMoney(c.price)}</div>
      <div class="mobile-card-field"><span>Acres</span>${fmtAcres(c.acreage)}</div>
      <div class="mobile-card-field"><span>$/Acre</span>${fmtMoney(c.price_per_acre)}</div>
      <div class="mobile-card-field"><span>Units</span>${hasValue(c.unit_count) ? Number(c.unit_count).toLocaleString() : '<span class="null-val">—</span>'}</div>
      <div class="mobile-card-field"><span>$/Unit</span>${fmtMoney(c.price_per_unit)}</div>
      <div class="mobile-card-field"><span>ELC Deal</span>${fmtStr(c.elc_deal)}</div>
      <div class="mobile-card-field"><span>Aerial</span>${fmtLink(c.aerial,'View Aerial')}</div>
      <div class="mobile-card-field"><span>Seller</span>${fmtStr(c.grantor)}</div>
      <div class="mobile-card-field"><span>Buyer</span>${fmtStr(c.grantee)}</div>
      <div class="mobile-card-field"><span>Source</span>${fmtStr(c.source)}</div>
      <div class="mobile-card-field"><span>Zoning</span>${fmtStr(c.zoning)}</div>
    </div>${comments}
  </div>`;
}
function renderPage(){
  const total=filteredComps.length, totalPages=Math.max(1,Math.ceil(total/PAGE_SIZE));
  currentPage=Math.min(currentPage,totalPages);
  const start=(currentPage-1)*PAGE_SIZE, end=Math.min(start+PAGE_SIZE,total);
  document.getElementById('row-count').textContent = total ? `Showing ${start+1}–${end} of ${total.toLocaleString()} comps` : 'No comps match the current filters';
  document.getElementById('page-info').textContent = `Page ${currentPage} of ${totalPages}`;
  document.getElementById('btn-prev').disabled=currentPage<=1;
  document.getElementById('btn-next').disabled=currentPage>=totalPages;
  const pageItems = filteredComps.slice(start,end);
  document.getElementById('table-body').innerHTML = pageItems.map(c=>`<tr class="${selectedCompKeys.has(c._compKey)?'row-selected':''}">${DISPLAY_COLUMNS.map(([key])=>`<td class="${key==='_select'?'checkbox-cell':''}">${fmtCell(c,key)}</td>`).join('')}</tr>`).join('');
  document.getElementById('mobile-cards').innerHTML = pageItems.map(buildMobileCard).join('');
  updateSelectedCount();
}
function changePage(dir){currentPage+=dir;renderPage();window.scrollTo({top:0,behavior:'smooth'});}
function loadLocalDeals(){
  try{return JSON.parse(localStorage.getItem(LOCAL_DEALS_KEY) || '[]');}catch(e){return [];}
}
function saveLocalDeals(deals){localStorage.setItem(LOCAL_DEALS_KEY, JSON.stringify(deals));}
function toNumOrNull(v){ if(v === null || v === undefined || String(v).trim()==='') return null; const n=Number(v); return Number.isFinite(n) ? n : null; }
function isoToDisplay(iso){
  if(!iso) return null;
  const d = new Date(iso + 'T00:00:00'); if(Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString('en-US',{month:'short',day:'2-digit',year:'numeric'}).replace(',', '');
}

function looksLikeMultifamilyComp(c){
  const text = [c.address,c.comments,c.zoning,c.land_use,c.property_type,c.deed_type,c.source].filter(hasValue).join(' ').toLowerCase();
  return /(multi[\s-]?family|apartment|apartments|\bapt\b|units?\b|duplex|triplex|quadplex|townhome|townhomes|condo|condominium|residential units?)/.test(text);
}
function consolidatePropertyType(c){
  const raw = String(c.property_type || '').trim();
  const lower = raw.toLowerCase();
  if(hasValue(c.unit_count) && Number(c.unit_count) > 0) return 'Multifamily';
  if(lower === 'investment' || lower === 'investment property'){
    return looksLikeMultifamilyComp(c) ? 'Multifamily' : 'Land';
  }
  return raw || 'Land';
}

function deriveCompFields(c){
  c.property_type = consolidatePropertyType(c);
  if(hasValue(c.price) && hasValue(c.acreage) && Number(c.acreage)!==0) c.price_per_acre = Math.round(Number(c.price)/Number(c.acreage));
  else c.price_per_acre = null;
  if(hasValue(c.price) && hasValue(c.unit_count) && Number(c.unit_count)!==0) c.price_per_unit = Math.round(Number(c.price)/Number(c.unit_count));
  else c.price_per_unit = null;
  if(c.sale_date_iso && !c.sale_date) c.sale_date = isoToDisplay(c.sale_date_iso);
  if(c.property_type === 'Multifamily' && !hasValue(c.unit_count)) c.unit_count = null;
  return c;
}
function openAddDealModal(){
  editingCompKey = null;
  document.getElementById('add-deal-title').textContent='Add New Deal';
  const submitBtn=document.querySelector('#add-deal-form button[type="submit"]'); if(submitBtn) submitBtn.textContent='Add Deal';
  const form=document.getElementById('add-deal-form'); if(form) form.reset();
  document.getElementById('new-property-type').value='Land';
  document.getElementById('new-transaction-type').value='Sale';
  document.getElementById('new-source').value='Manual Entry';
  document.getElementById('add-deal-modal').classList.add('open');
}
function closeAddDealModal(){ document.getElementById('add-deal-modal').classList.remove('open'); }
function readFileAsDataURL(file){
  return new Promise((resolve,reject)=>{const r=new FileReader(); r.onload=()=>resolve(r.result); r.onerror=reject; r.readAsDataURL(file);});
}
async function getAerialFromForm(){
  const pasted=document.getElementById('new-aerial-url').value.trim();
  if(pasted) return pasted;
  const file=document.getElementById('new-aerial-file').files[0];
  if(!file) return null;
  if(file.size > 1800000){ alert('That image is pretty large. For best performance, upload the image somewhere first and paste the URL, or use a smaller aerial image under about 1.8 MB.'); return null; }
  return await readFileAsDataURL(file);
}

function setFormValue(id, val){ const el=document.getElementById(id); if(el) el.value = hasValue(val) ? val : ''; }
function fillDealFormFromComp(c){
  setFormValue('new-property-type', c.property_type || 'Land');
  setFormValue('new-transaction-type', c.transaction_type || 'Sale');
  setFormValue('new-sale-date', c.sale_date_iso || '');
  setFormValue('new-elc-deal', c.elc_deal === 'Yes' ? 'Yes' : 'No');
  setFormValue('new-address', c.address); setFormValue('new-price', c.price); setFormValue('new-acreage', c.acreage); setFormValue('new-unit-count', c.unit_count);
  setFormValue('new-lat', c.lat); setFormValue('new-lon', c.lon); setFormValue('new-grantee', c.grantee); setFormValue('new-grantor', c.grantor);
  setFormValue('new-zoning', c.zoning); setFormValue('new-land-use', c.land_use); setFormValue('new-source', c.source || 'Manual Entry');
  setFormValue('new-aerial-url', c.aerial); setFormValue('new-deed-url', c.deed_url); setFormValue('new-comments', c.comments);
}
function getCompByKey(key){ return allComps.find(c=>c._compKey === key); }
function openEditDealModal(key){
  const comp = getCompByKey(key); if(!comp){ alert('Could not find that comp to edit.'); return; }
  editingCompKey = key;
  const form=document.getElementById('add-deal-form'); if(form) form.reset();
  fillDealFormFromComp(comp);
  document.getElementById('add-deal-title').textContent='Edit Deal Fields';
  const submitBtn=document.querySelector('#add-deal-form button[type="submit"]'); if(submitBtn) submitBtn.textContent='Save Changes';
  document.getElementById('add-deal-modal').classList.add('open');
}
async function saveDealFromForm(){
  if(editingCompKey) return await saveEditedDealFromForm();
  return await addNewDealFromForm();
}
async function saveEditedDealFromForm(){
  const existing = getCompByKey(editingCompKey); if(!existing){ alert('Could not find that comp to edit.'); return; }
  const aerial = await getAerialFromForm();
  const saleIso = document.getElementById('new-sale-date').value || null;
  let updated = {...existing,
    address: document.getElementById('new-address').value.trim() || null,
    sale_date: isoToDisplay(saleIso), sale_date_iso: saleIso,
    price: toNumOrNull(document.getElementById('new-price').value), acreage: toNumOrNull(document.getElementById('new-acreage').value),
    transaction_type: document.getElementById('new-transaction-type').value.trim() || 'Sale',
    grantor: document.getElementById('new-grantor').value.trim() || null, grantee: document.getElementById('new-grantee').value.trim() || null,
    property_type: document.getElementById('new-property-type').value || 'Land', zoning: document.getElementById('new-zoning').value.trim() || null,
    land_use: document.getElementById('new-land-use').value.trim() || null, source: document.getElementById('new-source').value.trim() || 'Manual Entry',
    comments: document.getElementById('new-comments').value.trim() || null,
    lat: toNumOrNull(document.getElementById('new-lat').value), lon: toNumOrNull(document.getElementById('new-lon').value), coords_approx:false,
    deed_url: document.getElementById('new-deed-url').value.trim() || null,
    aerial: aerial || existing.aerial || null,
    unit_count: toNumOrNull(document.getElementById('new-unit-count').value), elc_deal: document.getElementById('new-elc-deal').value === 'Yes' ? 'Yes' : null,
    edited_locally:true
  };
  updated = deriveCompFields(updated);
  saveCompEdit(updated);
  allComps = allComps.map(c=>c._compKey===editingCompKey ? updated : c);
  editingCompKey = null; closeAddDealModal(); refreshAfterDataChange(); alert('Changes saved in this browser.');
}

async function addNewDealFromForm(){
  const aerial = await getAerialFromForm();
  const nextNum = Math.max(0, ...allComps.map(c=>Number(c.num)||0)) + 1;
  const saleIso = document.getElementById('new-sale-date').value || null;
  let comp = {
    num: nextNum, parcel_id:null, parcel_raw_id:null,
    address: document.getElementById('new-address').value.trim() || null,
    sale_date: isoToDisplay(saleIso), sale_date_iso: saleIso,
    price: toNumOrNull(document.getElementById('new-price').value),
    acreage: toNumOrNull(document.getElementById('new-acreage').value),
    price_per_acre:null, book_page:null,
    transaction_type: document.getElementById('new-transaction-type').value.trim() || 'Sale',
    grantor: document.getElementById('new-grantor').value.trim() || null,
    grantee: document.getElementById('new-grantee').value.trim() || null,
    deed_type:null,
    property_type: document.getElementById('new-property-type').value || 'Land',
    zoning: document.getElementById('new-zoning').value.trim() || null,
    land_use: document.getElementById('new-land-use').value.trim() || null,
    source: document.getElementById('new-source').value.trim() || 'Manual Entry',
    comments: document.getElementById('new-comments').value.trim() || null,
    lat: toNumOrNull(document.getElementById('new-lat').value),
    lon: toNumOrNull(document.getElementById('new-lon').value),
    coords_approx:false,
    deed_url: document.getElementById('new-deed-url').value.trim() || null,
    aerial: aerial,
    unit_count: toNumOrNull(document.getElementById('new-unit-count').value),
    price_per_unit:null,
    elc_deal: document.getElementById('new-elc-deal').value === 'Yes' ? 'Yes' : null,
    manual_entry:true,
    _manual_id: 'm-' + Date.now() + '-' + Math.random().toString(36).slice(2,8)
  };
  comp = deriveCompFields(comp);
  const localDeals = loadLocalDeals(); localDeals.push(comp); saveLocalDeals(localDeals);
  allComps.push(comp);
  allComps = applyLocalEdits(allComps);
  populateFilters(); currentPage=1; applyFilters(); closeAddDealModal();
  alert('Deal added in this browser. It will show on the map if latitude and longitude were entered.');
}

function toggleCompSelection(key, checked){
  if(checked) selectedCompKeys.add(key); else selectedCompKeys.delete(key);
  updateSelectedCount(); renderPage();
}
function updateSelectedCount(){
  const el=document.getElementById('selected-count'); if(el) el.textContent = `${selectedCompKeys.size.toLocaleString()} selected`;
}
function selectVisibleComps(){
  const start=(currentPage-1)*PAGE_SIZE, end=Math.min(start+PAGE_SIZE, filteredComps.length);
  filteredComps.slice(start,end).forEach(c=>selectedCompKeys.add(c._compKey)); renderPage();
}
function selectAllFilteredComps(){ filteredComps.forEach(c=>selectedCompKeys.add(c._compKey)); renderPage(); }
function clearSelectedComps(){ selectedCompKeys.clear(); renderPage(); }
function getSelectedComps(){ return allComps.filter(c=>selectedCompKeys.has(c._compKey)); }

function reportMoneyText(n){ return hasValue(n) ? '$' + Math.round(Number(n)).toLocaleString() : '—'; }
function reportCountText(n){ return hasValue(n) ? Number(n).toLocaleString() : '—'; }
function reportDecimalText(n, digits=2){ return hasValue(n) ? Number(n).toLocaleString(undefined,{maximumFractionDigits:digits}) : '—'; }
function reportText(v){ return hasValue(v) ? escapeHtml(v) : '—'; }
function shortenLabel(label, max=24){ const s=String(label||''); return s.length>max ? s.slice(0,max-1)+'…' : s; }
function getTypeChartData(comps){
  const counts = new Map();
  comps.forEach(c=>{ const key = hasValue(c.property_type) ? String(c.property_type) : 'Unspecified'; counts.set(key, (counts.get(key)||0)+1); });
  return Array.from(counts.entries()).sort((a,b)=>b[1]-a[1]);
}
function getPriceBucketChartData(comps){
  const buckets = [
    ['Under $250k',0],['$250k–$500k',0],['$500k–$1M',0],['$1M–$3M',0],['Over $3M',0],['No Price',0]
  ];
  comps.forEach(c=>{
    if(!hasValue(c.price)){ buckets[5][1]++; return; }
    const p = Number(c.price);
    if(p < 250000) buckets[0][1]++;
    else if(p < 500000) buckets[1][1]++;
    else if(p < 1000000) buckets[2][1]++;
    else if(p < 3000000) buckets[3][1]++;
    else buckets[4][1]++;
  });
  return buckets.filter(b=>b[1] > 0);
}
function getScatterChartPoints(comps){
  return comps.filter(c=>hasValue(c.acreage) && hasValue(c.price) && Number(c.acreage) > 0 && Number(c.price) > 0).map(c=>({
    x: Number(c.acreage),
    y: Number(c.price),
    label: c.address || c.property_type || 'Comp'
  }));
}
function buildBarChartSvg(items, opts={}){
  if(!items.length) return '<div class="empty-chart">No chart data available.</div>';
  const width = opts.width || 560;
  const left = opts.left || 150;
  const right = opts.right || 60;
  const top = 14;
  const barH = 22;
  const gap = 10;
  const bottom = 16;
  const innerW = width - left - right;
  const maxVal = Math.max(...items.map(i=>Number(i[1])||0), 1);
  const height = top + bottom + items.length * (barH + gap);
  const valueFormatter = opts.valueFormatter || (v => Number(v).toLocaleString());
  const bars = items.map(([rawLabel, rawVal], i) => {
    const label = shortenLabel(rawLabel, 24);
    const val = Number(rawVal) || 0;
    const y = top + i * (barH + gap);
    const barW = Math.max(2, innerW * (val / maxVal));
    return `<text x="${left-10}" y="${y + 15}" font-size="11" text-anchor="end" fill="#344054">${escapeHtml(label)}</text>
      <rect x="${left}" y="${y}" width="${barW}" height="${barH}" rx="5" fill="#ef1f25"></rect>
      <text x="${Math.min(left + barW + 8, width - 4)}" y="${y + 15}" font-size="11" fill="#101828">${escapeHtml(valueFormatter(val))}</text>`;
  }).join('');
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="chart">${bars}</svg>`;
}
function buildScatterSvg(points){
  if(points.length < 2) return '<div class="empty-chart">Need at least two comps with both acreage and price to draw the scatter plot.</div>';
  const width = 560, height = 300;
  const pad = {l:58,r:18,t:16,b:42};
  const plotW = width - pad.l - pad.r, plotH = height - pad.t - pad.b;
  const maxX = Math.max(...points.map(p=>p.x), 1);
  const maxY = Math.max(...points.map(p=>p.y), 1);
  const toX = x => pad.l + (x / maxX) * plotW;
  const toY = y => pad.t + plotH - (y / maxY) * plotH;
  let axes = `<line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${pad.t+plotH}" stroke="#98a2b3"></line><line x1="${pad.l}" y1="${pad.t+plotH}" x2="${pad.l+plotW}" y2="${pad.t+plotH}" stroke="#98a2b3"></line>`;
  for(let i=0;i<=4;i++){
    const xv = maxX * (i/4), yv = maxY * (i/4);
    const x = toX(xv), y = toY(yv);
    axes += `<line x1="${x}" y1="${pad.t}" x2="${x}" y2="${pad.t+plotH}" stroke="#eaecf0"></line>`;
    axes += `<text x="${x}" y="${height-10}" font-size="10" text-anchor="middle" fill="#667085">${escapeHtml(reportDecimalText(xv,1))}</text>`;
    axes += `<line x1="${pad.l}" y1="${y}" x2="${pad.l+plotW}" y2="${y}" stroke="#eaecf0"></line>`;
    axes += `<text x="${pad.l-8}" y="${y+3}" font-size="10" text-anchor="end" fill="#667085">${escapeHtml(reportMoneyText(yv))}</text>`;
  }
  const pts = points.map(p=>`<circle cx="${toX(p.x)}" cy="${toY(p.y)}" r="5" fill="#ef1f25" fill-opacity="0.82"><title>${escapeHtml((p.label || 'Comp') + ' | ' + reportDecimalText(p.x,2) + ' ac | ' + reportMoneyText(p.y))}</title></circle>`).join('');
  const labels = `<text x="${pad.l + plotW/2}" y="${height-2}" font-size="11" text-anchor="middle" fill="#344054">Acres</text><text x="14" y="${pad.t + plotH/2}" font-size="11" text-anchor="middle" fill="#344054" transform="rotate(-90 14 ${pad.t + plotH/2})">Price</text>`;
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="scatter chart">${axes}${pts}${labels}</svg>`;
}
function buildTypeSummaryList(typeData){
  if(!typeData.length) return '<div class="empty-chart">No property type data available.</div>';
  return `<div class="mini-list">${typeData.slice(0,8).map(([label,val])=>`<div class="mini-list-row"><span>${escapeHtml(label)}</span><strong>${Number(val).toLocaleString()}</strong></div>`).join('')}</div>`;
}
function buildMapThumbnailUrl(comp){
  if(!hasValue(comp.lat) || !hasValue(comp.lon)) return null;
  const lat = Number(comp.lat), lon = Number(comp.lon);
  if(!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const latDelta = 0.0048;
  const lonDelta = latDelta / Math.max(Math.cos(lat * Math.PI/180), 0.2);
  const bbox = [lon-lonDelta, lat-latDelta, lon+lonDelta, lat+latDelta].join(',');
  return `https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export?bbox=${bbox}&bboxSR=4326&imageSR=4326&size=900,420&format=png32&transparent=false&f=image`;
}
function isLikelyImageUrl(url){
  if(!hasValue(url)) return false;
  const s = String(url).toLowerCase();
  return s.startsWith('data:image/') || /\.(jpg|jpeg|png|gif|webp|bmp|svg)(\?|#|$)/.test(s);
}
function buildCompDetailCard(comp, index){
  const mapUrl = buildMapThumbnailUrl(comp);
  const aerialUrl = hasValue(comp.aerial) ? String(comp.aerial) : null;
  const commentHtml = hasValue(comp.comments) ? escapeHtml(String(comp.comments)).replace(/\n/g,'<br>') : '<span class="muted">No comments added.</span>';
  const detailRows = [
    ['Property Type', reportText(comp.property_type)],
    ['Sale Date', reportText(comp.sale_date)],
    ['Price', reportMoneyText(comp.price)],
    ['Acres', reportDecimalText(comp.acreage,2)],
    ['$/Acre', reportMoneyText(comp.price_per_acre)],
    ['Units', reportCountText(comp.unit_count)],
    ['$/Unit', reportMoneyText(comp.price_per_unit)],
    ['Buyer', reportText(comp.grantee)],
    ['Seller', reportText(comp.grantor)],
    ['Zoning', reportText(comp.zoning)],
    ['Land Use', reportText(comp.land_use)],
    ['Source', reportText(comp.source)],
    ['ELC Deal', reportText(comp.elc_deal)]
  ].map(([label,val])=>`<div class="detail-row"><span>${label}</span><strong>${val}</strong></div>`).join('');
  const mapBlock = mapUrl ? `<div class="media-card"><div class="media-head">Aerial Location Map</div><div class="map-thumb"><img src="${escapeHtml(mapUrl)}" alt="Aerial location map for ${escapeHtml(comp.address || 'selected comp')}"><div class="map-pin"></div></div><div class="media-note">Centered on saved coordinates${hasValue(comp.lat)&&hasValue(comp.lon) ? `, ${Number(comp.lat).toFixed(5)}, ${Number(comp.lon).toFixed(5)}` : ''}.</div></div>` : `<div class="media-card empty-media"><div class="media-head">Aerial Location Map</div><div class="empty-state">No latitude/longitude available for this comp.</div></div>`;
  const aerialBlock = aerialUrl ? `<div class="media-card"><div class="media-head">Attached Aerial / Image</div><div class="photo-wrap">${isLikelyImageUrl(aerialUrl) ? `<img src="${escapeHtml(aerialUrl)}" alt="Aerial image for ${escapeHtml(comp.address || 'selected comp')}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">` : ''}<div class="empty-state" style="${isLikelyImageUrl(aerialUrl) ? 'display:none;' : 'display:flex;'}">Preview not available for this link.</div></div><div class="media-note"><a href="${escapeHtml(aerialUrl)}" target="_blank" rel="noopener noreferrer">Open aerial/image</a></div></div>` : `<div class="media-card empty-media"><div class="media-head">Attached Aerial / Image</div><div class="empty-state">No aerial/image link on file.</div></div>`;
  const deedLink = hasValue(comp.deed_url) ? `<a href="${escapeHtml(comp.deed_url)}" target="_blank" rel="noopener noreferrer">Open deed link</a>` : '<span class="muted">No deed link</span>';
  return `<section class="comp-report-card"><div class="comp-card-head"><div><div class="eyebrow">Comp ${index + 1}</div><h2>${reportText(comp.address)}</h2></div><div class="comp-head-stats"><span>${reportText(comp.property_type)}</span><strong>${reportMoneyText(comp.price)}</strong></div></div><div class="detail-grid">${detailRows}</div><div class="media-grid">${mapBlock}${aerialBlock}</div><div class="comments-block"><div class="comments-title">Comments</div><div class="comments-copy">${commentHtml}</div></div><div class="foot-links">${deedLink}</div></section>`;
}


function createSelectedReport(){
  const comps = getSelectedComps();
  if(!comps.length){ alert('Select at least one comp first.'); return; }
  const withPrice = comps.filter(c=>hasValue(c.price));
  const withAcres = comps.filter(c=>hasValue(c.acreage));
  const withPPA = comps.filter(c=>hasValue(c.price_per_acre));
  const withLatLon = comps.filter(c=>hasValue(c.lat) && hasValue(c.lon));
  const withAerial = comps.filter(c=>hasValue(c.aerial));
  const totalVolume = withPrice.reduce((s,c)=>s+Number(c.price),0);
  const totalAcres = withAcres.reduce((s,c)=>s+Number(c.acreage),0);
  const avgPrice = withPrice.length ? totalVolume / withPrice.length : 0;
  const avgPPA = withPPA.length ? withPPA.reduce((s,c)=>s+Number(c.price_per_acre),0) / withPPA.length : 0;
  const minPrice = withPrice.length ? Math.min(...withPrice.map(c=>Number(c.price))) : null;
  const maxPrice = withPrice.length ? Math.max(...withPrice.map(c=>Number(c.price))) : null;
  const typeData = getTypeChartData(comps);
  const priceBucketData = getPriceBucketChartData(comps);
  const scatterPoints = getScatterChartPoints(comps);
  const typeChart = buildBarChartSvg(typeData, { valueFormatter:v => `${v.toLocaleString()} comp${v===1?'':'s'}` });
  const priceBucketChart = buildBarChartSvg(priceBucketData, { valueFormatter:v => `${v.toLocaleString()} comp${v===1?'':'s'}` });
  const scatterChart = buildScatterSvg(scatterPoints);
  const detailCards = comps.map((c,i)=>buildCompDetailCard(c,i)).join('');
  const logoUrl = new URL('./EshenbaughLogo.png', window.location.href).href;
  const html = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Selected Comp Report</title>
<style>
:root{--red:#ef1f25;--ink:#17212f;--muted:#667085;--line:#d9e1ea;--bg:#f4f7fb;--white:#fff}
*{box-sizing:border-box}body{font-family:Arial,sans-serif;margin:0;color:var(--ink);background:var(--bg)}
.report-shell{max-width:1240px;margin:0 auto;padding:24px}
.top{background:#000;color:#fff;border-radius:16px;padding:22px 24px;margin-bottom:18px;display:flex;justify-content:space-between;gap:18px;align-items:flex-start}
.brand{display:flex;gap:18px;align-items:center}.brand img{width:235px;max-width:38vw;background:#fff;padding:8px 12px;border-radius:10px}.brand-copy h1{margin:0 0 8px;font-size:28px}.brand-copy p{margin:0;font-size:13px;line-height:1.5;opacity:.92}
.top-actions{display:flex;gap:10px;flex-wrap:wrap}.top-actions button{background:var(--red);color:#fff;border:0;border-radius:8px;padding:10px 14px;font-weight:700;cursor:pointer}
.metrics{display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:12px;margin:0 0 18px}.metric{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:0 1px 5px rgba(0,0,0,.05)}.metric .val{font-size:23px;font-weight:800;color:#101828}.metric .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-top:5px}
.report-grid{display:grid;grid-template-columns:1.15fr 1fr;gap:16px;margin-bottom:18px}.panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:0 1px 5px rgba(0,0,0,.05)}.panel h3{margin:0 0 12px;font-size:16px}.panel-copy{font-size:12px;color:var(--muted);line-height:1.5;margin-top:8px}
.mini-list{display:grid;gap:8px}.mini-list-row{display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:8px 10px;border:1px solid #edf1f5;border-radius:8px;background:#fafbfd}.mini-list-row strong{color:#101828}.empty-chart{min-height:120px;display:flex;align-items:center;justify-content:center;border:1px dashed #cfd7e3;border-radius:10px;color:var(--muted);font-size:12px;padding:14px;text-align:center}
.comp-report-card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px;margin:0 0 18px;box-shadow:0 1px 6px rgba(0,0,0,.05);break-inside:avoid-page;page-break-inside:avoid}.comp-card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;border-bottom:1px solid #edf1f5;padding-bottom:12px;margin-bottom:14px}.comp-card-head h2{margin:4px 0 0;font-size:22px;line-height:1.25}.eyebrow{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.6px;color:var(--red)}.comp-head-stats{display:grid;gap:6px;text-align:right;min-width:150px}.comp-head-stats span{font-size:11px;text-transform:uppercase;color:var(--muted);letter-spacing:.6px}.comp-head-stats strong{font-size:22px;color:#101828}
.detail-grid{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:10px 14px;margin-bottom:14px}.detail-row{border:1px solid #edf1f5;border-radius:10px;padding:9px 10px;background:#fbfcfe}.detail-row span{display:block;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:4px}.detail-row strong{font-size:12px;line-height:1.35;color:#101828}
.media-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}.media-card{border:1px solid #edf1f5;border-radius:14px;padding:12px;background:#fff}.media-head{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.55px;color:var(--muted);margin-bottom:8px}.map-thumb,.photo-wrap{position:relative;height:240px;border-radius:12px;overflow:hidden;background:#eef2f7;border:1px solid #e6ebf1;display:flex;align-items:center;justify-content:center}.map-thumb img,.photo-wrap img{width:100%;height:100%;object-fit:cover;display:block}.map-pin{position:absolute;left:50%;top:50%;width:18px;height:18px;background:var(--red);border:3px solid #fff;border-radius:50%;transform:translate(-50%,-50%);box-shadow:0 1px 10px rgba(0,0,0,.35)}.map-pin:after{content:'';position:absolute;left:50%;top:85%;transform:translateX(-50%);width:4px;height:18px;background:var(--red);border-radius:3px}
.media-note{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.4}.media-note a{color:var(--red);font-weight:800;text-decoration:none}.media-note a:hover{text-decoration:underline}.empty-media .photo-wrap,.empty-media .map-thumb{background:#f8fafc}.empty-state{height:100%;width:100%;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:12px;padding:16px}
.comments-block{border:1px solid #edf1f5;border-radius:12px;padding:12px;background:#fbfcfe}.comments-title{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.55px;color:var(--muted);margin-bottom:6px}.comments-copy{font-size:13px;line-height:1.55;color:#344054}.muted{color:var(--muted)}.foot-links{margin-top:10px;font-size:12px}.foot-links a{color:var(--red);font-weight:800;text-decoration:none}.foot-links a:hover{text-decoration:underline}
.section-title{margin:0 0 12px;font-size:20px}.subtle{color:var(--muted);font-size:12px;line-height:1.5}
@media(max-width:1050px){.metrics{grid-template-columns:repeat(3,minmax(140px,1fr))}.report-grid{grid-template-columns:1fr}.detail-grid{grid-template-columns:repeat(2,minmax(150px,1fr))}.media-grid{grid-template-columns:1fr}.brand{flex-direction:column;align-items:flex-start}.top{flex-direction:column}}
@media(max-width:640px){.report-shell{padding:12px}.top{padding:16px;border-radius:12px}.brand img{max-width:260px;width:100%}.brand-copy h1{font-size:22px}.metrics{grid-template-columns:repeat(2,minmax(120px,1fr))}.detail-grid{grid-template-columns:1fr}.comp-card-head{flex-direction:column}.comp-head-stats{text-align:left}}
@media print{body{background:#fff}.report-shell{max-width:none;padding:0}.top-actions{display:none}.top,.metric,.panel,.comp-report-card{box-shadow:none}.top{border-radius:0}.panel,.metric,.comp-report-card{break-inside:avoid-page;page-break-inside:avoid}}
</style></head><body><div class="report-shell">
<div class="top"><div class="brand"><img src="${logoUrl}" alt="Eshenbaugh Land Company" onerror="this.style.display='none'"><div class="brand-copy"><h1>Selected Comp Report</h1><p>${new Date().toLocaleDateString()} | ${comps.length.toLocaleString()} selected comps<br>Includes summary metrics, charts, aerial-style locator maps, attached aerial/image previews, and comments when available.</p></div></div><div class="top-actions"><button onclick="window.print()">Print / Save as PDF</button></div></div>
<div class="metrics">
  <div class="metric"><div class="val">${comps.length.toLocaleString()}</div><div class="lbl">Selected Comps</div></div>
  <div class="metric"><div class="val">${reportMoneyText(totalVolume)}</div><div class="lbl">Total Volume</div></div>
  <div class="metric"><div class="val">${reportMoneyText(avgPrice)}</div><div class="lbl">Average Price</div></div>
  <div class="metric"><div class="val">${reportDecimalText(totalAcres,1)}</div><div class="lbl">Total Acres</div></div>
  <div class="metric"><div class="val">${reportMoneyText(avgPPA)}</div><div class="lbl">Average $/Acre</div></div>
  <div class="metric"><div class="val">${withAerial.length.toLocaleString()} / ${withLatLon.length.toLocaleString()}</div><div class="lbl">With Image / With Map</div></div>
</div>
<div class="report-grid">
  <div class="panel"><h3>Property Type Breakdown</h3>${typeChart}<div class="panel-copy">Quick view of what is in the selected set by property type.</div></div>
  <div class="panel"><h3>Mix Snapshot</h3>${buildTypeSummaryList(typeData)}<div class="panel-copy">Price range: <strong>${reportMoneyText(minPrice)}</strong> to <strong>${reportMoneyText(maxPrice)}</strong><br>Comps with comments: <strong>${comps.filter(c=>hasValue(c.comments)).length.toLocaleString()}</strong><br>Comps with attached aerial/image: <strong>${withAerial.length.toLocaleString()}</strong></div></div>
</div>
<div class="report-grid">
  <div class="panel"><h3>Sale Price Distribution</h3>${priceBucketChart}<div class="panel-copy">Counts grouped into broad sale price buckets for the selected comps.</div></div>
  <div class="panel"><h3>Acres vs. Price</h3>${scatterChart}<div class="panel-copy">Scatter plot using comps that have both acreage and price populated.</div></div>
</div>
<h2 class="section-title">Comp Detail Pages</h2><p class="subtle">Each selected comp below includes its primary deal details, an aerial-style location map based on coordinates when available, any attached aerial/image preview, and comments.</p>
${detailCards}
</div></body></html>`;
  const w = window.open('', '_blank');
  if(!w){ alert('Your browser blocked the report popup. Allow popups and try again.'); return; }
  w.document.open();
  w.document.write(html);
  w.document.close();
}

function openImportModal(){ document.getElementById('import-form').reset(); document.getElementById('import-default-property-type').value='Land'; document.getElementById('import-default-source').value='Imported Excel'; document.getElementById('import-preview').textContent='No file selected yet.'; document.getElementById('import-modal').classList.add('open'); }
function closeImportModal(){ document.getElementById('import-modal').classList.remove('open'); }
function normalizeHeader(h){ return String(h||'').toLowerCase().replace(/[^a-z0-9]+/g,'').trim(); }
const IMPORT_ALIASES={address:['address','propertyaddress','siteaddress'],sale_date:['saledate','date','closingdate'],price:['price','saleprice','purchaseprice'],acreage:['acreage','acres','landsize','landsizeac'],property_type:['propertytype','type'],transaction_type:['transactiontype','transaction'],grantor:['seller','grantor'],grantee:['buyer','grantee'],zoning:['zoning'],land_use:['landuse','futurelanduse','flu'],source:['source'],comments:['comments','notes','description'],lat:['lat','latitude'],lon:['lon','lng','long','longitude'],aerial:['aerial','aerialurl','photourl','photo','imageurl'],deed_url:['deedurl','deed'],unit_count:['units','unitcount'],elc_deal:['elcdeal','elc']};
function getImportVal(row, field){ const aliases=IMPORT_ALIASES[field]||[field]; for(const [k,v] of Object.entries(row)){ if(aliases.includes(normalizeHeader(k))) return v; } return null; }
function importDateToISO(v){ if(!hasValue(v)) return null; if(typeof v==='number' && window.XLSX && XLSX.SSF){ const d=XLSX.SSF.parse_date_code(v); if(d) return `${d.y}-${String(d.m).padStart(2,'0')}-${String(d.d).padStart(2,'0')}`; } const d=new Date(v); return Number.isNaN(d.getTime()) ? null : d.toISOString().slice(0,10); }
function makeImportedComp(row, idx){
  const saleIso=importDateToISO(getImportVal(row,'sale_date'));
  let c={num:Math.max(0,...allComps.map(c=>Number(c.num)||0))+idx+1,parcel_id:null,parcel_raw_id:null,address:String(getImportVal(row,'address')||'').trim()||null,sale_date:isoToDisplay(saleIso),sale_date_iso:saleIso,price:toNumOrNull(getImportVal(row,'price')),acreage:toNumOrNull(getImportVal(row,'acreage')),price_per_acre:null,book_page:null,transaction_type:String(getImportVal(row,'transaction_type')||'Sale').trim(),grantor:String(getImportVal(row,'grantor')||'').trim()||null,grantee:String(getImportVal(row,'grantee')||'').trim()||null,deed_type:null,property_type:String(getImportVal(row,'property_type')||document.getElementById('import-default-property-type').value||'Land').trim(),zoning:String(getImportVal(row,'zoning')||'').trim()||null,land_use:String(getImportVal(row,'land_use')||'').trim()||null,source:String(getImportVal(row,'source')||document.getElementById('import-default-source').value||'Imported Excel').trim(),comments:String(getImportVal(row,'comments')||'').trim()||null,lat:toNumOrNull(getImportVal(row,'lat')),lon:toNumOrNull(getImportVal(row,'lon')),coords_approx:false,deed_url:String(getImportVal(row,'deed_url')||'').trim()||null,aerial:String(getImportVal(row,'aerial')||'').trim()||null,unit_count:toNumOrNull(getImportVal(row,'unit_count')),price_per_unit:null,elc_deal:String(getImportVal(row,'elc_deal')||'').toLowerCase().startsWith('y')?'Yes':null,manual_entry:true,_manual_id:'imp-'+Date.now()+'-'+idx};
  return deriveCompFields(c);
}
async function importDealsFromFile(){
  const file=document.getElementById('import-file').files[0]; if(!file){ alert('Choose an Excel or CSV file first.'); return; }
  if(!window.XLSX){ alert('The Excel importer did not load. Check your internet connection, refresh, and try again.'); return; }
  const buf=await file.arrayBuffer(); const wb=XLSX.read(buf,{type:'array',cellDates:false}); const ws=wb.Sheets[wb.SheetNames[0]]; const rows=XLSX.utils.sheet_to_json(ws,{defval:null});
  const imported=rows.filter(r=>Object.values(r).some(hasValue)).map((r,i)=>makeImportedComp(r,i+1)).filter(c=>hasValue(c.address)||hasValue(c.price)||hasValue(c.acreage));
  if(!imported.length){ alert('No usable deal rows were found. Make sure the first row has column headers.'); return; }
  const localDeals=loadLocalDeals(); saveLocalDeals([...localDeals,...imported]); allComps=applyLocalEdits([...allComps,...imported]); closeImportModal(); populateFilters(); currentPage=1; applyFilters(); alert(`${imported.length.toLocaleString()} deals imported in this browser.`);
}
document.addEventListener('change', async function(e){
  if(e.target && e.target.id==='import-file'){
    const f=e.target.files[0]; const el=document.getElementById('import-preview');
    if(!f){ el.textContent='No file selected yet.'; return; }
    el.textContent=`Ready to import: ${f.name}. The first sheet will be used.`;
  }
});

function resetFilters(){
  ['search','field-value','sale-date-from','sale-date-to','min-price','max-price','min-acres','max-acres','min-ppa','max-ppa','min-units','max-units','min-ppu','max-ppu'].forEach(id=>document.getElementById(id).value='');
  ['field-filter','filter-type','filter-source','filter-elc','filter-transaction','filter-aerial'].forEach(id=>document.getElementById(id).value='');
  currentPage=1;toggleUnitPriceFilters();applyFilters();
}
loadReport();
