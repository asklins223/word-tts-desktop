const fs = require('node:fs');
const path = require('node:path');
const renderer = path.resolve(__dirname, '../../renderer');
const html = fs.readFileSync(path.join(renderer, 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script\s+src="([^"]+)"><\/script>/g)].map(match => match[1]).filter(script => !['app.js', 'ui-components.js', 'vendor/wavesurfer.min.js'].includes(script));
const fixtureRegionTree = [
    { id: 440000, name: '广东省', level: 1, parentId: null, children: [
        { id: 440100, name: '广州市', level: 2, parentId: 440000, children: [
            { id: 440106, name: '天河区', level: 3, parentId: 440100, children: [] },
            { id: 440111, name: '白云区', level: 3, parentId: 440100, children: [] },
        ] },
        { id: 440300, name: '深圳市', level: 2, parentId: 440000, children: [
            { id: 440305, name: '南山区', level: 3, parentId: 440300, children: [] },
        ] },
    ] },
    { id: 430000, name: '湖南省', level: 1, parentId: null, children: [
        { id: 430100, name: '长沙市', level: 2, parentId: 430000, children: [
            { id: 430102, name: '芙蓉区', level: 3, parentId: 430100, children: [] },
        ] },
    ] },
];
function workspace(inputType = 'paper', count = 3) {
    const textbook = { textbookNameZh: '单元对话', textbookNameEn: 'Getting to know you', textbookForm: '同步课文', textbookVersion: '人教版', textbookStage: '初中', textbookGrade: '七年级', textbookVolume: '上册', textbookUnit: 'Unit 1', textbookLesson: 'Section A' };
    const paper = { paperName: '单元听说测试', paperCategory: '题型专项', provinceId: { id: 440000, name: '广东省' }, cityId: { id: 440100, name: '广州市' }, districtIds: [], stageId: { id: 2, name: '初中' }, gradeId: { id: 7, name: '七年级' }, year: 2026, answerTimeMinutes: 5, platformTemplateName: '模仿朗读' };
    return { snapshot: { workflow_id: 'target-editor-fixture', source_artifact_id: 'source-fixture', state_version: 1 }, configuration: { configuration_revision: 1 }, items: [], system_input: { available: true, configuration_editable: true, input_type_capabilities: [{input_type:'paper',label:'试卷',external_supported:true},{input_type:'textbook',label:'课文',external_supported:true},{input_type:'vocabulary',label:'词汇',external_supported:false,status:'reserved',reason:'当前录入类型的页面适配器尚未接入'}], delivery_mode: 'audio_only', input_type: inputType, document_entry_support: { supported: true }, units: Array.from({ length: count }, (_, index) => ({ unit_id: `unit-${index + 1}`, ordinal: index, label: inputType === 'paper' ? `第 ${index + 1} 套 · Unit ${index + 1} 听说测试` : `Unit ${index + 1} · Section A 对话`, input_type: inputType, source_range: { start: index, end: index }, configuration: { ...(inputType === 'paper' ? paper : textbook), unit_id: `unit-${index + 1}`, ...(inputType === 'paper' ? { paperName: `Unit ${index + 1} 听说测试` } : { textbookNameZh: `Unit ${index + 1} 对话`, textbookUnit: `Unit ${index + 1}` }) } })), content_segments: Array.from({ length: count }, (_, index) => ({ unit_id: `unit-${index + 1}`, segment_id: `seg-${index + 1}`, raw_text: `M: Hello! What is your name?\nW: My name is Emma. Nice to meet you.`, ordinal: index })) } };
}
function prelude() {
    return `window.__saved = []; window.__templates = []; window.__messages = []; window.__saveFailure = false;
    window.__platformTemplates = [
        {platform_template_key: 'fixture:mimic', input_type: 'paper', name: '模仿朗读', platform_template_version: '1'},
        {platform_template_key: 'fixture:response', input_type: 'paper', name: '听后应答', platform_template_version: '1'},
        {platform_template_key: 'fixture:answer', input_type: 'paper', name: '听后回答', platform_template_version: '1'},
        {platform_template_key: 'fixture:selection', input_type: 'paper', name: '听后选择', platform_template_version: '1'},
    ];
    window.electronAPI = { platform: 'darwin', workflow: {
        sendCommand: async () => { throw new Error('Fixture does not execute workflow commands'); },
        listSystemInputTemplates: async () => window.__templates,
        listPlatformInputTemplates: async () => ({templates: window.__platformTemplates}),
        getWorkspace: async () => window.__workspace,
        saveSystemInputConfiguration: async (id, request) => {
            window.__saved.push(request);
            if (window.__saveFailure) throw new Error('模拟保存失败');
            window.__workspace.system_input.units.forEach(unit => { unit.configuration = request.configuration.units.find(item => item.unit_id === unit.unit_id); });
            window.__workspace.system_input.delivery_mode = request.configuration.delivery_mode;
            window.__workspace.snapshot.state_version++;
            return {workspace: window.__workspace};
        },
        createSystemInputTemplate: async (payload) => { const template = {...payload, app_template_id: 'template-1'}; window.__templates.push(template); return template; }
    }};`;
}
function setup(data, { regionTree = fixtureRegionTree } = {}) {
    return `window.__workspace = ${JSON.stringify(data)};
        currentWorkspace = window.__workspace;
        currentSession = {session_id: currentWorkspace.snapshot.workflow_id, state_version: 1, source_filename: '七年级上册听说练习.docx'};
        reviewDocumentEntrySupport = () => ({supported: true});
        showToast = (message, tone) => { window.__messages.push({message, tone}); };
        showConfirmDialog = async () => true;
        showPromptDialog = async () => '我的常用方案';
        systemInputRegionTree = ${JSON.stringify(regionTree)};
        systemInputRegionIndexes = flattenSystemInputRegions(systemInputRegionTree);
        systemInputPlatformTemplates = [...window.__platformTemplates];
        systemInputTextbookCatalog = {records: Array.from({length: 10}, (_, i) => ({version:{name:'人教版'},stage:{name:'初中'},grade:{name:'七年级'},volume:{name:'上册'},unit:{name:'Unit '+(i+1)},lesson:{name:'Section A'}}))};
        ensureSystemInputDeliveryPanel();
        openSystemInputConfigDrawer(currentWorkspace);
        window.__readTargets = () => collectSystemInputConfiguration(currentWorkspace.system_input).units;
    `;
}
module.exports = { renderer, html, scripts, workspace, prelude, setup, fixtureRegionTree };
