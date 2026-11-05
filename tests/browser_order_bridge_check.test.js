'use strict';

// Pure Node 22 contracts. Synthetic files only; no Odoo, Chrome, HTTP or secrets.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ROOT = path.resolve(__dirname, '..');
const SCRIPT = path.join(ROOT, 'scripts/browser_order_bridge_check.js');
const ROLES = ['settings', 'operator', 'integration', 'sales', 'other_sales'];
const OLD = ['sales', 'manager', 'manager2', 'buyer', 'warehouse', 'finance', 'other_sales'];
const SOURCE = 'portfolio.import.source', JOB = 'portfolio.import.job', EVENT = 'portfolio.import.attempt';
const MODELS = {sources: SOURCE, customer_maps: 'portfolio.import.customer.map',
  product_maps: 'portfolio.import.product.map', jobs: JOB, attempts: EVENT, sales: 'sale.order'};
const MENUS = {settings: ['sources', 'customer_maps', 'product_maps'], operator: ['jobs', 'attempts', 'sales'],
  integration: ['jobs', 'attempts'], sales: ['sales'], other_sales: ['sales']};
const COMMON = ['session', 'groups', 'menus', 'actions', 'views', 'healthy_after_negatives', 'business_unchanged',
  'cross_company', 'private_rpc_denied', 'no_scope_spoof', 'audit_crud_denied', 'scoped_queries'];
const CHECKS = {settings: ['service_denied', 'job_acl_denied', 'cron_configuration', 'source_catalog'],
  operator: ['source_private_fields_denied', 'queue_empty', 'done_retry_denied', 'quote_details'],
  integration: ['service_denied', 'native_so_denied', 'mappings_denied', 'source_private_fields_denied', 'idempotent_reception'],
  sales: ['service_denied', 'job_acl_denied', 'approval_guard', 'quote_details'],
  other_sales: ['service_denied', 'job_acl_denied', 'foreign_quotes_hidden']};
const GROUPS = ['base.group_user', 'base.group_system', 'base.group_erp_manager',
  'portfolio_order_bridge.group_import_operator', 'portfolio_order_bridge.group_import_integration',
  'sales_team.group_sale_salesman', 'sales_team.group_sale_salesman_all_leads', 'sales_team.group_sale_manager',
  'purchase.group_purchase_user', 'stock.group_stock_user', 'account.group_account_invoice'];
const ROOT_MENUS = {trade: 'portfolio_trade.menu_portfolio_root', import: 'portfolio_order_bridge.menu_import_root', config: 'portfolio_order_bridge.menu_import_config'};
const FORBIDDEN_TRADE = ['purchases', 'stock', 'invoices'].map(key => `portfolio_trade.menu_portfolio_${key}`);
const PAGE_KEYS = {settings: ['source', 'customer_maps', 'product_maps', 'cron'],
  operator: ['job_success', 'job_repaired', 'attempts', 'audit_success', 'audit_repaired', 'quote_success', 'quote_repaired'],
  integration: ['job_success', 'job_repaired', 'attempts', 'audit_success', 'audit_repaired'],
  sales: ['quote_success', 'quote_repaired'], other_sales: ['sales']};
const clone = value => structuredClone(value);
function api() {
  assert.ok(fs.existsSync(SCRIPT), 'Five-role rendered-browser verifier is missing');
  const result = require(SCRIPT);
  for (const name of ['parseArgs', 'readInputs', 'validateSourceReports', 'validateRoleReport',
    'validateDomEvidence', 'validateBrowserReport', 'classifyBusinessRows', 'clickVisibleRecord', 'safeFailure', 'main']) {
    assert.equal(typeof result[name], 'function', `Missing browser contract: ${name}`);
  }
  return result;
}
function identities(models, start) {
  return Object.fromEntries(Object.entries(models).map(([key, model], i) => [key, {model, id: start + i}]));
}
function fixture() {
  const oldModels = {company_a: 'res.company', company_b: 'res.company', warehouse_a: 'stock.warehouse',
    customer: 'res.partner', vendor: 'res.partner', initial_bearing_move: 'stock.move',
    ...Object.fromEntries(OLD.map(r => [`user_${r}`, 'res.users'])),
    ...Object.fromEntries(['bearing', 'sensor', 'bolt'].map(r => [`product_${r}`, 'product.product'])),
    ...Object.fromEntries(['bearing', 'sensor', 'bolt'].map(r => [`supplier_${r}`, 'product.supplierinfo']))};
  const old = identities(oldModels, 101);
  const counts = Object.values(oldModels).reduce((all, key) => ({...all, [key]: (all[key] || 0) + 1}), {});
  const acceptance = {namespace: 'portfolio_trade_acceptance', identities: old, record_counts: counts,
    bearing_quantity: 40, bearing_move_count: 1, initial_bearing_move_id: old.initial_bearing_move.id};
  const statics = identities({...Object.fromEntries(ROLES.slice(0, 3).map(r => [`user_${r}`, 'res.users'])),
    customer: 'res.partner', vendor: 'res.partner', product_item1: 'product.product', product_item2: 'product.product',
    template_item1: 'product.template', template_item2: 'product.template', supplier_item1: 'product.supplierinfo',
    supplier_item2: 'product.supplierinfo', source: SOURCE, customer_map: MODELS.customer_maps,
    product_map_item1: MODELS.product_maps, product_map_item2: MODELS.product_maps,
    processing_cron: 'ir.cron', processing_server_action: 'ir.actions.server'}, 501);
  const profile = {schema_version: 1, database: 'odoo_portfolio', namespace: 'portfolio_order_bridge_acceptance',
    identities: statics, company_id: old.company_a.id, source_id: statics.source.id,
    cron_active: false, borrowed_m1_identities: clone(old), borrowed_finance_pricelist_id: 600};
  const ids = identities({job_success: JOB, quote_success: 'sale.order', attempt_success: EVENT, job_repaired: JOB,
    quote_repaired: 'sale.order', attempt_failed: EVENT, attempt_retry: EVENT, attempt_repaired: EVENT,
    product_map_repair: MODELS.product_maps}, 701);
  const flow = {schema_version: 1, database: 'odoo_portfolio', namespace: 'portfolio_order_bridge_flow',
    company_id: old.company_a.id, source_id: statics.source.id, identities: ids, jobs: {}, quotes: {}, events: {},
    m1_preservation: {fingerprint: 'a'.repeat(64), borrowed_m1_identities: clone(old), bearing_stock: 10,
      bearing_move_ids: [old.initial_bearing_move.id, 1901, 1902]}};
  for (const [suffix, total, count, name] of [['success', 309, 1, 'S00002'], ['repaired', 161.5, 2, 'S00003']]) {
    flow.jobs[suffix] = {id: ids[`job_${suffix}`].id, state: 'done', attempt_count: count,
      submitted_by_id: statics.user_integration.id, processed_by_id: statics.user_operator.id};
    flow.quotes[suffix] = {id: ids[`quote_${suffix}`].id, name, state: 'draft', currency: 'CNY', amount_untaxed: total,
      amount_total: total, owner_id: old.user_sales.id, created_by_id: statics.user_operator.id, approval_required: true};
  }
  for (const [key, suffix, kind, no] of [['attempt_success', 'success', 'done', 1], ['attempt_failed', 'repaired', 'failed', 1],
    ['attempt_retry', 'repaired', 'retry', 1], ['attempt_repaired', 'repaired', 'done', 2]]) {
    flow.events[key] = {id: ids[key].id, job_id: ids[`job_${suffix}`].id, event_type: kind, attempt_no: no,
      actor_id: statics.user_operator.id, retry_reason: kind === 'retry' ? 'Acceptance SKU mapping repaired' : null};
  }
  const roles = {};
  for (const role of ROLES) {
    const active = ['base.group_user', ...({settings: ['base.group_system', 'base.group_erp_manager'],
      operator: ['portfolio_order_bridge.group_import_operator', 'sales_team.group_sale_salesman'],
      integration: ['portfolio_order_bridge.group_import_integration'], sales: ['sales_team.group_sale_salesman'],
      other_sales: ['sales_team.group_sale_salesman']}[role])];
    const company = old[role === 'other_sales' ? 'company_b' : 'company_a'].id;
    const keys = MENUS[role];
    roles[role] = {uid: (ROLES.indexOf(role) < 3 ? statics : old)[`user_${role}`].id, company_id: company,
      allowed_company_ids: [company], active_company_ids: [company], groups: Object.fromEntries(GROUPS.map(g => [g, active.includes(g)])),
      menus: Object.fromEntries(keys.map(k => [k, {id: 901 + Object.keys(MODELS).indexOf(k),
        action_id: 951 + Object.keys(MODELS).indexOf(k), model: MODELS[k]}])),
      actions: Object.fromEntries(keys.map(k => [k, {id: 951 + Object.keys(MODELS).indexOf(k), res_model: MODELS[k], view_mode: 'list,form'}])),
      views: Object.fromEntries(keys.map(k => [MODELS[k], {list: true, form: true}])),
      menu_roots: {trade: {id: 880, parent_id: false}, ...(ROLES.indexOf(role) < 3 ? {import: {id: 881, parent_id: 880}} : {}),
        ...(role === 'settings' ? {config: {id: 882, parent_id: 881}} : {})},
      source_id: ROLES.indexOf(role) < 3 ? statics.source.id : null,
      mapping_ids: ['settings', 'operator'].includes(role) ? {customer: [statics.customer_map.id],
        product: [statics.product_map_item1.id, statics.product_map_item2.id, ids.product_map_repair.id]} : {},
      cron: role === 'settings' ? {id: statics.processing_cron.id, active: false,
        processor_uid: statics.user_operator.id, fixed_code: true, source_link: true} : null,
      jobs: ['operator', 'integration'].includes(role) ? clone(flow.jobs) : {},
      quotes: ['operator', 'sales'].includes(role) ? clone(flow.quotes) : {},
      events: ['operator', 'integration'].includes(role) ? clone(flow.events) : {},
      checks: Object.fromEntries([...COMMON, ...CHECKS[role]].map(k => [k, true]))};
  }
  return {database: 'odoo_portfolio', acceptance, profile, flow,
    roleReport: {schema_version: 1, ok: true, database: 'odoo_portfolio', profile_source_id: statics.source.id,
      roles, business_facts: {jobs: 2, draft_quotes: 2, attempt_events: 4, quote_totals_cny: [309, 161.5],
        preserved_m1_identities: 19, bearing_stock: 10}}};
}
const F = (value, extra = {}) => ({visible: true, value, ...extra});
function dom(role, inputs = fixture()) {
  const source = 'Portfolio bridge acceptance source', operator = 'Portfolio bridge acceptance operator';
  const integration = 'Portfolio bridge acceptance integration', sales = 'Acceptance sales';
  const company = 'PARTS-A';
  const page = (key, model, details) => ({visible: true, model, screenshot: `${role}-${key}.png`, ...details});
  const state = {role, login: {rendered_form: true, submitted: true, authenticated: true,
    user_label: ROLES.indexOf(role) < 3 ? `Portfolio bridge acceptance ${role}` : `Acceptance ${role}`,
    password_field_count: 0}, menu_xmlids: [ROOT_MENUS.trade, ...(ROLES.indexOf(role) < 3 ? [ROOT_MENUS.import] : []),
      ...MENUS[role].map(k => k === 'sales' ? 'portfolio_trade.menu_portfolio_sales' :
      `portfolio_order_bridge.menu_import_${k}`)], import_payload_entry_count: 0, import_root_visible: ROLES.indexOf(role) < 3, pages: {}};
  state.config_headers = role === 'settings' ? [{visible: true, label: 'Import settings / 导入配置',
    root_xmlid: ROOT_MENUS.import, root_open: true, header_has_xmlid: false,
    child_xmlids: ['sources', 'customer_maps', 'product_maps'].map(key => `portfolio_order_bridge.menu_import_${key}`)}] : [];
  if (role === 'settings') {
    state.pages.source = page('source', SOURCE, {fields: {name: F(source), company_id: F(company),
      company_code: F('PARTS-A'), code: F('B2B-ACCEPTANCE'), sales_user_id: F(sales), processor_user_id: F(operator),
      integration_user_ids: F([integration]), processing_cron_id: F(`Order import / 订单导入: ${source}`, {linked: true})}});
    state.pages.customer_maps = page('customer_maps', MODELS.customer_maps, {columns: ['source_id', 'company_id', 'external_ref', 'partner_id', 'active'],
      rows: [{visible: true, source_id: source, company_id: company, external_ref: 'BRIDGE-ACCEPTANCE-CUSTOMER',
        partner_id: 'Portfolio bridge acceptance customer', active: true}]});
    state.pages.product_maps = page('product_maps', MODELS.product_maps, {columns: ['source_id', 'company_id', 'external_sku', 'product_id', 'active'],
      rows: ['1', '2', 'REPAIR'].map(n => ({visible: true, source_id: source, company_id: company, external_sku: `BRIDGE-ACCEPTANCE-${n}`,
        product_id: `Portfolio bridge acceptance item ${n === '1' ? 1 : 2}`, active: true}))});
    state.pages.cron = page('cron', 'ir.cron', {opened_by_link: true, fields: {name: F(`Order import / 订单导入: ${source}`),
      active: F(false), user_id: F(operator), code: F('model._process_pending()')}});
  }
  if (['operator', 'integration'].includes(role)) {
    state.job_list_buttons = role === 'operator' ? ['action_process_pending'] : [];
    for (const suffix of ['success', 'repaired']) {
      const key = `job_${suffix}`, ext = suffix === 'success' ? 'BRIDGE-ACCEPTANCE-SUCCESS' : 'BRIDGE-ACCEPTANCE-RETRY';
      const events = Object.entries(inputs.flow.events).filter(([, e]) => e.job_id === inputs.flow.jobs[suffix].id)
        .map(([event_key, e]) => ({visible: true, event_key, attempt_no: String(e.attempt_no), event_type: e.event_type,
          actor: operator, retry_reason: e.retry_reason || ''}));
      state.pages[key] = page(key, JOB, {readonly: true, write_controls: [], visible_buttons: role === 'operator' ? ['action_open_sale_order'] : [],
        opened_from_visible_row: true, fields: {external_order_id: F(ext), source_id: F(source), company_id: F(company),
          state: F('done'), attempt_count: F(String(inputs.flow.jobs[suffix].attempt_count)), submitted_by_id: F(integration),
          sale_order_id: F(inputs.flow.quotes[suffix].name, {readonly: true, no_open: true})},
        metadata: {visible: true, creator: integration}, history: events});
      state.pages[`audit_${suffix}`] = page(`audit_${suffix}`, JOB, {readonly: true,
        fields: {raw_payload: F(null, {readonly: true, nonempty: true}), canonical_payload: F(null, {readonly: true, nonempty: true}),
          payload_hash: F(null, {readonly: true, nonempty: true})}});
    }
    state.pages.attempts = page('attempts', EVENT, {readonly: true, write_controls: [], rows:
      Object.entries(inputs.flow.events).map(([event_key, e]) => ({visible: true, event_key,
        job: e.job_id === inputs.flow.jobs.success.id ? 'BRIDGE-ACCEPTANCE-SUCCESS' : 'BRIDGE-ACCEPTANCE-RETRY',
        attempt_no: String(e.attempt_no), event_type: e.event_type, actor: operator, retry_reason: e.retry_reason || ''}))});
  }
  if (['operator', 'sales'].includes(role)) for (const suffix of ['success', 'repaired']) {
    const key = `quote_${suffix}`, success = suffix === 'success';
    state.pages[key] = page(key, 'sale.order', {opened_from_visible_row: true, fields: {name: F(inputs.flow.quotes[suffix].name),
      partner_id: F('Portfolio bridge acceptance customer'), user_id: F(sales), state: F('draft'),
      origin: F(`B2B-ACCEPTANCE:BRIDGE-ACCEPTANCE-${success ? 'SUCCESS' : 'RETRY'}`),
      client_order_ref: F(`B2B-ACCEPTANCE:BRIDGE-ACCEPTANCE-${success ? 'SUCCESS' : 'RETRY'}`),
      portfolio_approval_state: F('draft'),
      tax_totals: F(success ? '¥ 309.00' : '¥ 161.50', {readonly: true, widget: 'account-tax-totals-field'})},
      metadata: {visible: true, creator: operator}, company_scope: {visible: true, label: company, origin: 'navbar'},
      approval_required: {visible: true, value: true, submit_button_visible: true, approval_tab_visible: true},
      lines: (success ? [[2, 3, 35, 0], [1, 2, 120, 15]] : [[1, 1, 120, 15], [2, 2, 35, 15]])
        .map(([product, qty, unit, discount]) => ({visible: true, product: `Portfolio bridge acceptance item ${product}`,
          product_visible: true, unit: '', unit_visible: false, unit_evidence: {visible: false, origin: 'native_feature_disabled', native_column_absent: true},
          quantity: String(qty), price_unit: String(unit), subtotal: String(qty * unit * (1 - discount / 100)),
          discount: '', discount_evidence: {visible: false, origin: 'visible_quantity_price_subtotal', derived_percent: discount, native_column_absent: true},
          tax: '', visibility: {quantity: true, price_unit: true, subtotal: true, discount: false, tax: true}}))});
  }
  if (role === 'other_sales') state.pages.sales = page('sales', 'sale.order', {record_names: [], has_native_sample_data: false,
    sample_row_count: 0, foreign_quotes_absent: true, empty_state: {visible: true, selector: '.o_view_nocontent .o_nocontent_help', text: 'No data to display'}});
  return state;
}
function files(t) {
  const dir = fs.mkdtempSync(path.join(ROOT, '.runtime', 'browser-bridge-unit-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const input = fixture(), paths = {};
  for (const [key, value] of Object.entries({acceptanceReport: input.acceptance, profileReport: input.profile,
    flowReport: input.flow, roleReport: input.roleReport,
    credentials: {schema_version: 1, database: input.database, accounts: Object.fromEntries(ROLES.slice(0, 3).map(r =>
      [r, {login: `portfolio.bridge.acceptance.${r}`, password: `SYNTHETIC-ONLY-${r}-` + 'x'.repeat(28)}]))},
    salesCredentials: {schema_version: 1, database: input.database, accounts: Object.fromEntries(OLD.map(r =>
      [r, {login: `portfolio.acceptance.${r}`, password: `SYNTHETIC-ONLY-${r}-` + 'y'.repeat(28)}]))}})) {
    paths[key] = path.join(dir, `${key}.json`); fs.writeFileSync(paths[key], JSON.stringify(value), {mode: 0o600});
  }
  const options = {baseUrl: 'http://127.0.0.1:8069', database: input.database, ...paths, outputDir: path.join(dir, 'output')};
  return {input, paths, options, argv: Object.entries(options).flatMap(([k, v]) => [`--${k.replace(/[A-Z]/g, c => '-' + c.toLowerCase())}`, v])};
}

test('import is inert: no process, connection, or private file read', () => {
  assert.ok(fs.existsSync(SCRIPT), 'Five-role rendered-browser verifier is missing');
  const source = fs.readFileSync(SCRIPT, 'utf8');
  const deny = () => assert.fail('Import performed an external action');
  const sandbox = {module: {exports: {}}, exports: {}, __dirname: path.dirname(SCRIPT), Buffer,
    process: {argv: [], env: {}, getuid: process.getuid}, require: name => name === 'node:child_process' ? {spawn: deny} :
      name === 'node:fs' ? new Proxy(fs, {get: (target, key) => key === 'readFileSync' ? deny : target[key]}) :
      name === 'node:http' || name === 'node:https' ? {request: deny, get: deny} : require(name), WebSocket: deny};
  sandbox.require.main = undefined;
  vm.runInNewContext(source, sandbox, {filename: SCRIPT});
});

test('CLI requires complete flags and canonical local target before browser setup', t => {
  const {argv} = files(t), a = api();
  assert.equal(a.parseArgs(argv).database, 'odoo_portfolio');
  for (const bad of [argv.concat('--password', 'SYNTHETIC-SECRET'), argv.concat('--database', 'odoo_portfolio'), argv.slice(2),
    argv.map(v => v === 'http://127.0.0.1:8069' ? 'http://localhost:8069' : v),
    argv.map(v => v === 'http://127.0.0.1:8069' ? 'http://127.0.0.1:8069/' : v),
    argv.map(v => v === 'http://127.0.0.1:8069' ? 'http://127.0.0.1:65536' : v),
    argv.map(v => v === 'odoo_portfolio' ? 'production' : v)]) assert.throws(() => a.parseArgs(bad));
});

test('all synthetic inputs validate without browser or HTTP', t => {
  const {options} = files(t), inputs = api().readInputs(options);
  assert.equal(inputs.flow.quotes.success.amount_total, 309);
  assert.equal(inputs.credentials.accounts.operator.login, 'portfolio.bridge.acceptance.operator');
});

test('canonical runtime boundaries reject traversal, symlinks, and output input aliases', t => {
  const {options, paths} = files(t), a = api();
  const linked = path.join(path.dirname(paths.credentials), 'linked.json'); fs.symlinkSync(paths.credentials, linked);
  const hardlink = path.join(path.dirname(paths.credentials), 'hardlink.json'); fs.linkSync(paths.credentials, hardlink);
  for (const change of [{credentials: linked}, {credentials: paths.credentials.replace('credentials.json', '../' + path.basename(path.dirname(paths.credentials)) + '/credentials.json')},
    {credentials: hardlink, salesCredentials: paths.credentials}, {outputDir: paths.credentials}, {outputDir: ROOT},
    {outputDir: options.outputDir + '/../output'}]) assert.throws(() => a.readInputs({...options, ...change}));
  fs.mkdirSync(options.outputDir); fs.linkSync(paths.flowReport, path.join(options.outputDir, 'report.json'));
  assert.throws(() => a.readInputs(options));
});

test('both credential contracts enforce mode, owner, exact roles and logins, and unique secrets', t => {
  const {options, paths} = files(t), a = api();
  for (const key of ['credentials', 'salesCredentials']) {
    const original = fs.readFileSync(paths[key], 'utf8');
    fs.chmodSync(paths[key], 0o644); assert.throws(() => a.readInputs(options)); fs.chmodSync(paths[key], 0o600);
    const value = JSON.parse(original), roles = Object.keys(value.accounts);
    for (const mutate of [v => { v.schema_version = true; }, v => { delete v.accounts[roles[0]]; },
      v => { v.accounts[roles[0]].login = 'admin'; }, v => { v.accounts[roles[0]].password = v.accounts[roles[1]].password; },
      v => { v.accounts[roles[0]].extra = 'unexpected'; }]) {
      const bad = clone(value); mutate(bad); fs.writeFileSync(paths[key], JSON.stringify(bad)); assert.throws(() => a.readInputs(options));
    }
    fs.writeFileSync(paths[key], original.replace('"schema_version":1', '"schema_version":1,"schema_version":1'));
    assert.throws(() => a.readInputs(options), 'Duplicate JSON keys must fail'); fs.writeFileSync(paths[key], original);
  }
});

test('source identities, totals, ownership, actors, cron, and M1 preservation are strict', () => {
  const a = api(); a.validateSourceReports(fixture());
  for (const mutate of [x => { delete x.profile.identities.processing_cron; }, x => { x.flow.identities.job_success.id = true; },
    x => { x.flow.jobs.success.attempt_count = 2; }, x => { x.flow.quotes.success.amount_total = 309.01; },
    x => { x.flow.quotes.repaired.owner_id = x.profile.identities.user_operator.id; }, x => { x.profile.cron_active = true; },
    x => { x.flow.events.attempt_retry.retry_reason = ''; }, x => { x.flow.events.attempt_repaired.actor_id = x.profile.identities.user_integration.id; },
    x => { x.flow.m1_preservation.bearing_stock = 9; }, x => { x.acceptance.record_counts['res.users'] = 8; },
    x => { x.profile.identities.customer.id = x.acceptance.identities.customer.id; }]) {
    const input = fixture(); mutate(input); assert.throws(() => a.validateSourceReports(input));
  }
});

test('HTTP report requires all five roles, every check, complete views, matching actions, full scope and business facts', () => {
  const a = api(); a.validateRoleReport(fixture().roleReport, fixture());
  for (const role of ROLES) {
    for (const check of [...COMMON, ...CHECKS[role]]) {
      const input = fixture(); delete input.roleReport.roles[role].checks[check];
      assert.throws(() => a.validateRoleReport(input.roleReport, input), `${role} must require ${check}`);
    }
    for (const mutate of [x => { delete x.roles[role]; }, x => { x.roles[role].groups['base.group_system'] = !x.roles[role].groups['base.group_system']; },
      x => { x.roles[role].active_company_ids = [999]; }, x => { delete x.roles[role].views[MODELS[MENUS[role][0]]].form; },
      x => { x.roles[role].menus[MENUS[role][0]].action_id += 1; }, x => { x.roles[role].actions[MENUS[role][0]].res_model = 'res.users'; }]) {
      const input = fixture(); mutate(input.roleReport); assert.throws(() => a.validateRoleReport(input.roleReport, input));
    }
  }
  const input = fixture(); input.roleReport.business_facts.jobs = 3; assert.throws(() => a.validateRoleReport(input.roleReport, input));
});

test('complete DOM evidence produces safe five-role rendered proof with all required pages', () => {
  const input = fixture(), a = api(), evidence = Object.fromEntries(ROLES.map(r => [r, dom(r, input)]));
  const report = a.validateBrowserReport(evidence, input);
  assert.equal(report.ok, true); assert.equal(report.verification, 'automated rendered browser');
  assert.deepEqual(Object.keys(report.roles).sort(), [...ROLES].sort());
  for (const role of ROLES) assert.deepEqual(Object.keys(report.roles[role].pages).sort(), [...PAGE_KEYS[role]].sort());
  const raw = JSON.stringify(report); assert.ok(!raw.includes('SYNTHETIC-ONLY')); assert.ok(!raw.includes('raw_payload'));
});

test('a role or required rendered page cannot be omitted or substituted', () => {
  const a = api(), input = fixture();
  for (const role of ROLES) for (const key of PAGE_KEYS[role]) {
    const evidence = Object.fromEntries(ROLES.map(r => [r, dom(r, input)])); delete evidence[role].pages[key];
    assert.throws(() => a.validateBrowserReport(evidence, input), `${role}/${key}`);
  }
  const evidence = Object.fromEntries(ROLES.slice(1).map(r => [r, dom(r, input)]));
  assert.throws(() => a.validateBrowserReport(evidence, input));
});

test('rendered login, password removal and screenshot filename whitelist are enforced', () => {
  const a = api(), input = fixture();
  for (const mutate of [x => { x.login.rendered_form = false; }, x => { x.login.password_field_count = 1; },
    x => { x.login.user_label = 'Administrator'; }, x => { x.pages.source.screenshot = '../credentials.json'; },
    x => { x.pages.source.visible = false; }, x => { x.import_payload_entry_count = 1; }]) {
    const state = dom('settings', input); mutate(state); assert.throws(() => a.validateDomEvidence('settings', state, input));
  }
});

test('settings native source, all mappings including repair, and linked inactive cron must be visibly proved', () => {
  const a = api(), input = fixture();
  for (const mutate of [x => { x.pages.source.fields.processor_user_id.value = 'Administrator'; },
    x => { x.pages.source.fields.code.visible = false; }, x => { x.pages.customer_maps.rows[0].external_ref = 'OTHER'; },
    x => { x.pages.product_maps.rows.pop(); }, x => { x.pages.product_maps.rows[2].product_id = 'Other item'; },
    x => { x.pages.cron.fields.active.value = true; }, x => { x.pages.cron.opened_by_link = false; },
    x => { x.pages.source.fields.processing_cron_id.linked = false; }]) {
    const state = dom('settings', input); mutate(state); assert.throws(() => a.validateDomEvidence('settings', state, input));
  }
});

test('both audit readers require done jobs, readonly raw helpers, native Integration creator, and exact historical events', () => {
  const a = api(), input = fixture();
  for (const role of ['operator', 'integration']) for (const mutate of [x => { x.pages.job_repaired.fields.attempt_count.value = '1'; },
    x => { x.pages.job_success.metadata.creator = 'Portfolio bridge acceptance operator'; },
    x => { x.pages.job_success.fields.submitted_by_id.visible = false; }, x => { x.pages.job_success.history = []; },
    x => { x.pages.job_repaired.history[1].retry_reason = ''; }, x => { x.pages.attempts.rows.pop(); },
    x => { x.pages.audit_repaired.fields.raw_payload.readonly = false; },
    x => { x.pages.job_repaired.visible_buttons.push('action_open_retry'); },
    x => { x.pages.job_success.write_controls.push('edit'); }]) {
    const state = dom(role, input); mutate(state); assert.throws(() => a.validateDomEvidence(role, state, input));
  }
  const integration = dom('integration', input); integration.pages.job_success.visible_buttons.push('action_open_sale_order');
  assert.throws(() => a.validateDomEvidence('integration', integration, input));
});

test('both quotation readers prove native tax totals, source, owner, creator, approval and visible line numbers and units', () => {
  const a = api(), input = fixture();
  for (const role of ['operator', 'sales']) for (const mutate of [x => { x.pages.quote_success.fields.tax_totals.value = '¥ 300.00'; },
    x => { x.pages.quote_success.fields.tax_totals.widget = 'amount_total'; }, x => { x.pages.quote_success.fields.tax_totals.readonly = false; },
    x => { x.pages.quote_repaired.fields.user_id.value = 'Administrator'; }, x => { x.pages.quote_success.fields.origin.visible = false; },
    x => { x.pages.quote_repaired.metadata.creator = 'Portfolio bridge acceptance integration'; },
    x => { x.pages.quote_success.lines[0].quantity = '4'; }, x => { x.pages.quote_repaired.lines[0].tax = '15%'; },
    x => { x.pages.quote_success.lines[0].unit_evidence.origin = 'report'; }, x => { x.pages.quote_success.approval_required.value = false; },
    x => { x.pages.quote_success.opened_from_visible_row = false; },
    x => { x.pages.quote_success.company_scope.label = 'PARTS-B'; }, x => { x.pages.quote_success.company_scope.origin = 'report'; },
    x => { x.pages.quote_success.approval_required.submit_button_visible = false; },
    x => { x.pages.quote_success.approval_required.approval_tab_visible = false; }]) {
    const state = dom(role, input); mutate(state); assert.throws(() => a.validateDomEvidence(role, state, input));
  }
});

test('Company B requires real empty-state proof; sample placeholders never count as business rows', () => {
  const a = api(), input = fixture(), empty = dom('other_sales', input).pages.sales.empty_state;
  assert.deepEqual(a.classifyBusinessRows(['Sample quotation'], true, empty, 'other_sales', input),
    {record_names: [], has_native_sample_data: true, sample_row_count: 1});
  assert.throws(() => a.classifyBusinessRows(['S00002'], true, empty, 'other_sales', input));
  assert.throws(() => a.classifyBusinessRows(['Sample quotation'], true, {...empty, visible: false}, 'other_sales', input));
  for (const mutate of [x => { x.pages.sales.record_names = ['S00003']; }, x => { x.pages.sales.empty_state.visible = false; },
    x => { x.menu_xmlids.push('portfolio_order_bridge.menu_import_jobs'); }, x => { x.import_root_visible = true; }]) {
    const state = dom('other_sales', input); mutate(state); assert.throws(() => a.validateDomEvidence('other_sales', state, input));
  }
});

test('exact visible row lookup and click occur atomically after a stale list', async () => {
  const a = api(); let clicks = 0, polls = 0;
  const makeCell = (value, visible) => ({textContent: value, closest: () => null, matches: () => false, querySelector: () => null, getClientRects: () => visible ? [{}] : [],
    click: () => { clicks += 1; }});
  const page = {wait: async expression => {
    for (let i = 0; i < 3; i++) {
      const cells = i === 0 ? [makeCell('S00002', false), makeCell('S000020', true)] : [makeCell('S00002', true)];
      polls++; const value = vm.runInNewContext(expression, {document: {querySelectorAll: () => cells},
        getComputedStyle: () => ({visibility: 'visible'})}); if (value) return value;
    }
    throw new Error('Expected visible target not clicked');
  }};
  await a.clickVisibleRecord(page, 'name', 'S00002'); assert.equal(clicks, 1); assert.equal(polls, 2);
});

test('failure report discards arbitrary errors, pages, credentials and Chrome stderr', () => {
  const report = api().safeFailure({role: 'operator', stage: 'job'}, new Error('SYNTHETIC-SECRET RAW-PAYLOAD SESSION DEBUG'));
  assert.equal(report.ok, false); assert.match(report.reason, /^[A-Z_]+$/);
  assert.ok(!JSON.stringify(report).includes('SYNTHETIC')); assert.ok(!JSON.stringify(report).includes('RAW-PAYLOAD'));
});

test('invalid inputs stop main before Chrome resolution, process spawn, HTTP, or output writes', async t => {
  const {argv, options, paths} = files(t), a = api(), cp = require('node:child_process');
  fs.chmodSync(paths.credentials, 0o644);
  const spawn = cp.spawn, write = process.stdout.write, output = [];
  try {
    cp.spawn = () => assert.fail('Invalid input launched Chrome');
    process.stdout.write = value => { output.push(String(value)); return true; };
    assert.equal(await a.main(argv), 1);
  } finally { cp.spawn = spawn; process.stdout.write = write; }
  assert.equal(JSON.parse(output.join('')).ok, false); assert.ok(!fs.existsSync(options.outputDir));
  assert.ok(!output.join('').includes('SYNTHETIC-ONLY'));
});


test('native product display prefixes keep exact item and SKU identity', () => {
  const a = api(), inputs = fixture(), state = dom('operator', inputs);
  for (const suffix of ['success', 'repaired']) for (const line of state.pages[`quote_${suffix}`].lines) {
    const item = line.product.endsWith('1') ? 1 : 2;
    line.product = `[BRIDGE-ACCEPTANCE-${item}] ${line.product}`;
  }
  a.validateDomEvidence('operator', state, inputs);
  state.pages.quote_success.lines[0].product = '[OTHER-SKU] Portfolio bridge acceptance item 2';
  assert.throws(() => a.validateDomEvidence('operator', state, inputs));
});

test('five role pages own separate native browser contexts and sessions, disposing failures too', async () => {
  const a = api(); assert.equal(typeof a.withRolePage, 'function');
  const calls = [], contexts = new Set(), sessions = new Set(); let next = 0;
  const cdp = {call: async (method, params, sessionId) => {
    calls.push({method, params, sessionId});
    if (method === 'Target.createBrowserContext') return {browserContextId: `context-${++next}`};
    if (method === 'Target.createTarget') return {targetId: `target-${params.browserContextId}`};
    if (method === 'Target.attachToTarget') return {sessionId: `session-${params.targetId}`};
    return {};
  }};
  for (const role of ROLES) await a.withRolePage(cdp, async page => {
    sessions.add(page.sessionId); contexts.add(calls.findLast(c => c.method === 'Target.createTarget').params.browserContextId);
    return role;
  });
  assert.equal(contexts.size, 5); assert.equal(sessions.size, 5);
  assert.equal(calls.filter(c => c.method === 'Target.disposeBrowserContext').length, 5);
  await assert.rejects(a.withRolePage(cdp, async () => { throw new Error('SYNTHETIC-FAILURE'); }));
  assert.equal(calls.filter(c => c.method === 'Target.disposeBrowserContext').length, 6);
});

function auditDom(readonly = true) {
  const fields = Object.fromEntries(['raw_payload', 'canonical_payload', 'payload_hash'].map(name => [name, {
    name, textContent: `SYNTHETIC-PRIVATE-${name}`, getClientRects: () => [{}],
    classList: {contains: name => readonly && name === 'o_readonly_modifier'},
    matches: () => false, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
    replaceChildren(node) { this.textContent = node.textContent; }
  }]));
  const form = {getClientRects: () => [{}], classList: {contains: () => true}, querySelector: () => null,
    querySelectorAll: selector => Object.values(fields).filter(el => selector.includes(`name="${el.name}"`))};
  const document = {querySelector: () => form, createTextNode: value => ({textContent: value})};
  return {fields, evaluate: expression => vm.runInNewContext(expression, {document,
    getComputedStyle: () => ({visibility: 'visible'})})};
}

test('raw helper inspection returns booleans only and redacts pixels after original readonly proof', async () => {
  const a = api(); assert.equal(typeof a.inspectAndRedactAudit, 'function');
  const dom = auditDom(); const page = {evaluate: async expression => dom.evaluate(expression),
    wait: async expression => { assert.equal(dom.evaluate(expression), true); }};
  const result = await a.inspectAndRedactAudit(page);
  assert.equal(result.fields.raw_payload.nonempty, true); assert.equal(result.fields.raw_payload.readonly, true);
  assert.ok(!JSON.stringify(result).includes('SYNTHETIC-PRIVATE'));
  assert.ok(Object.values(dom.fields).every(el => el.textContent === '[Audit content redacted]'));
  const editable = auditDom(false);
  await assert.rejects(a.inspectAndRedactAudit({evaluate: async expression => editable.evaluate(expression), wait: async () => true}));
  assert.ok(Object.values(editable.fields).every(el => el.textContent.startsWith('SYNTHETIC-PRIVATE')));
});


test('Settings proves exactly one visible dedicated Integration user tag on native Source', () => {
  const a = api(), input = fixture();
  for (const bad of [[], ['Administrator'], ['Portfolio bridge acceptance integration', 'Administrator']]) {
    const state = dom('settings', input); state.pages.source.fields.integration_user_ids.value = bad;
    assert.throws(() => a.validateDomEvidence('settings', state, input));
  }
  const hidden = dom('settings', input); hidden.pages.source.fields.integration_user_ids.visible = false;
  assert.throws(() => a.validateDomEvidence('settings', hidden, input));
});

test('Integration list header cannot expose processing or retry controls even with clean done forms', () => {
  const a = api(), input = fixture();
  for (const name of ['action_process_pending', 'action_open_retry']) {
    const state = dom('integration', input); state.job_list_buttons = [name];
    assert.throws(() => a.validateDomEvidence('integration', state, input));
  }
  const omitted = dom('integration', input); delete omitted.job_list_buttons;
  assert.throws(() => a.validateDomEvidence('integration', omitted, input));
  const operator = dom('operator', input); operator.job_list_buttons = [];
  assert.throws(() => a.validateDomEvidence('operator', operator, input));
});

test('all Portfolio leaves and import root/config visibility are retained before role policy validation', async () => {
  const a = api(); assert.equal(typeof a.collectVisibleMenus, 'function');
  const known = [ROOT_MENUS.trade, ROOT_MENUS.import, ROOT_MENUS.config, ...FORBIDDEN_TRADE,
    'portfolio_trade.menu_portfolio_sales', 'portfolio_order_bridge.menu_import_jobs'];
  const elements = known.map(value => ({dataset: {menuXmlid: value}, getClientRects: () => [{}]}));
  const actual = await a.collectVisibleMenus({evaluate: async expression => vm.runInNewContext(expression,
    {document: {querySelectorAll: () => elements}, getComputedStyle: () => ({visibility: 'visible'})})});
  assert.deepEqual([...actual].sort(), [...known].sort());
  const input = fixture();
  for (const extra of [ROOT_MENUS.config, ...FORBIDDEN_TRADE]) {
    const state = dom('integration', input); state.menu_xmlids.push(extra);
    assert.throws(() => a.validateDomEvidence('integration', state, input));
  }
});


test('native feature gating uses exact visible subtotal arithmetic and never fabricates discount or UOM columns', () => {
  const a = api(), input = fixture();
  for (const role of ['operator', 'sales']) {
    const actual = dom(role, input); a.validateDomEvidence(role, actual, input);
    for (const mutate of [x => { delete x.subtotal; }, x => { x.subtotal = '106'; },
      x => { x.visibility.subtotal = false; }, x => { x.discount_evidence.derived_percent = 15; },
      x => { x.discount_evidence.visible = true; }, x => { x.discount_evidence.origin = 'report'; },
      x => { x.discount_evidence.native_column_absent = false; }, x => { x.unit_evidence.native_column_absent = false; },
      x => { x.unit_evidence.origin = 'assumed'; }, x => { x.unit_visible = true; }]) {
      const state = dom(role, input); mutate(state.pages.quote_success.lines[0]);
      assert.throws(() => a.validateDomEvidence(role, state, input));
    }
    const visible = dom(role, input);
    for (const suffix of ['success', 'repaired']) for (const line of visible.pages[`quote_${suffix}`].lines) {
      line.unit = 'Units'; line.unit_visible = true;
      line.unit_evidence = {visible: true, origin: 'native_column', native_column_absent: false};
      line.discount = String(line.discount_evidence.derived_percent); line.visibility.discount = true;
      line.discount_evidence = {visible: true, origin: 'native_column', native_column_absent: false};
    }
    a.validateDomEvidence(role, visible, input);
    visible.pages.quote_success.lines[0].unit = 'Other unit';
    assert.throws(() => a.validateDomEvidence(role, visible, input));
  }
});


test('native nested Import settings header is proved without inventing a config XMLID attribute', async () => {
  const a = api(); assert.equal(typeof a.collectImportHeaders, 'function');
  const children = ['sources', 'customer_maps', 'product_maps'].map(key => ({
    dataset: {menuXmlid: `portfolio_order_bridge.menu_import_${key}`}, getClientRects: () => [{}],
    classList: {contains: () => false}, matches: () => false
  }));
  children.forEach((child, index) => { child.nextElementSibling = children[index + 1] || null; });
  const header = {textContent: 'Import settings / 导入配置', getClientRects: () => [{}], dataset: {},
    matches: () => false, querySelector: () => null, getAttribute: () => null,
    nextElementSibling: children[0], closest: () => ({getClientRects: () => [{}]})};
  const root = {dataset: {menuXmlid: ROOT_MENUS.import}, getClientRects: () => [{}], classList: {contains: name => name === 'show'}};
  const page = {evaluate: async expression => vm.runInNewContext(expression, {document: {
    querySelectorAll: selector => selector.includes('dropdown-header') ? [header] : [root]},
    getComputedStyle: () => ({visibility: 'visible'})})};
  const actual = await a.collectImportHeaders(page);
  assert.equal(actual.length, 1); assert.equal(actual[0].header_has_xmlid, false);
  assert.deepEqual([...actual[0].child_xmlids], children.map(child => child.dataset.menuXmlid));
  const input = fixture(), state = dom('settings', input); state.config_headers = JSON.parse(JSON.stringify(actual));
  a.validateDomEvidence('settings', state, input);
  for (const mutate of [x => { x.config_headers[0].root_open = false; }, x => { x.config_headers[0].child_xmlids.pop(); },
    x => { x.config_headers[0].label = 'Wrong header'; }, x => { x.config_headers[0].header_has_xmlid = true; },
    x => { x.menu_xmlids.push(ROOT_MENUS.config); }]) {
    const bad = dom('settings', input); mutate(bad); assert.throws(() => a.validateDomEvidence('settings', bad, input));
  }
  for (const role of ['operator', 'integration', 'sales', 'other_sales']) {
    const bad = dom(role, input); bad.config_headers = dom('settings', input).config_headers;
    assert.throws(() => a.validateDomEvidence(role, bad, input));
  }
});

test('cron reads rendered Ace lines while ignoring its hidden empty input, rejecting absent or wrong visible code', async () => {
  const a = api(); assert.equal(typeof a.collectCronCode, 'function');
  const render = (values, hidden = false) => {
    const hiddenInput = {value: '', getClientRects: () => [{}]};
    const lines = values.map(value => ({textContent: value, getClientRects: () => hidden ? [] : [{}]}));
    const widget = {getClientRects: () => [{}], querySelectorAll: selector => selector === '.ace_line' ? lines : [],
      querySelector: () => hiddenInput};
    const form = {getClientRects: () => [{}], querySelectorAll: () => [widget]};
    const document = {querySelector: () => form, querySelectorAll: () => [form]};
    return {wait: async expression => {
      const result = vm.runInNewContext(expression, {document, getComputedStyle: () => ({visibility: 'visible'})});
      if (!result) throw new Error('Rendered code unavailable'); return result;
    }};
  };
  const actual = await a.collectCronCode(render(['model._process_pending()']));
  assert.equal(actual.visible, true); assert.equal(actual.value, 'model._process_pending()');
  assert.equal(actual.origin, 'visible_ace_lines');
  for (const page of [render([]), render(['model._process_pending()'], true), render(['model.unlink()'])]) {
    await assert.rejects(a.collectCronCode(page));
  }
});

test('readonly audit list permits native search controls and rejects every editable business cell', async () => {
  const a = api(); assert.equal(typeof a.auditListReadonly, 'function');
  const check = async ({tag = null, readonly = false, disabled = false, hidden = false, searchClass = false} = {}) => {
    const search = {tagName: 'INPUT', type: 'text', className: 'o_searchview_input o_input', readOnly: false,
      disabled: false, getClientRects: () => [{}]};
    const cells = tag ? [{tagName: tag, type: 'text', className: searchClass ? 'o_searchview_input o_input' : 'o_input',
      readOnly: readonly, disabled, isContentEditable: tag === 'DIV', getClientRects: () => hidden ? [] : [{}]}] : [];
    const rows = Array.from({length: 4}, (_, index) => ({getClientRects: () => [{}],
      querySelectorAll: () => index === 0 ? cells : []}));
    const document = {querySelectorAll: selector => {
      if (selector.includes('tr.o_data_row')) return selector.includes('input') ? cells : rows;
      return [search, ...cells];
    }};
    return a.auditListReadonly({evaluate: async expression => vm.runInNewContext(expression,
      {document, getComputedStyle: () => ({visibility: 'visible'})})});
  };
  assert.equal(await check(), true, 'Visible search input is compatible with readonly business rows');
  for (const tag of ['INPUT', 'TEXTAREA', 'DIV']) {
    assert.equal(await check({tag}), false, `${tag} in a business row is editable`);
    assert.equal(await check({tag, searchClass: true}), false, 'Search CSS class cannot exempt a business cell');
  }
  assert.equal(await check({tag: 'INPUT', readonly: true}), true);
  assert.equal(await check({tag: 'TEXTAREA', disabled: true}), true);
  assert.equal(await check({tag: 'INPUT', hidden: true}), true);
});
