"""Fixed read-only native SQL collector; importing this module opens no connections."""

import hashlib
import re

from release_runtime import (
    DATABASE,
    GOLDEN,
    IDENTITIES,
    MODULE_VERSIONS,
    PG_OPTIONS,
    ReleaseError,
    digest,
    isolated_pg_environment,
    load_local_config,
    target_database,
    validate_snapshot,
)


def connect_readonly(config, database=None, purpose=None):
    # Re-read the private approved source configuration before importing libpq.
    config = load_local_config(config.path)
    database = target_database(database or config.database, purpose)
    try:
        with isolated_pg_environment():
            import psycopg2

            connection = psycopg2.connect(
                host="127.0.0.1", hostaddr="127.0.0.1", port=config.db_port,
                dbname=database, user=config.db_user, password=config.db_password,
                connect_timeout=5, sslmode="disable", options=PG_OPTIONS,
            )
        connection.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(),host(inet_server_addr()),inet_server_port(),"
                           "current_setting('server_version'),rolsuper,rolcreaterole,rolbypassrls,rolcreatedb "
                           "FROM pg_roles WHERE rolname=current_user")
            row = cursor.fetchone()
            if (not row or row[:3] != (database, "127.0.0.1", 55432)
                    or not re.match(r"16(?:\.|$)", row[3]) or any(row[4:7]) or row[7] is not True):
                raise ReleaseError("invalid_target")
        return connection
    except ReleaseError:
        if "connection" in locals():
            connection.close()
        raise
    except Exception:
        if "connection" in locals():
            connection.close()
        raise ReleaseError("database_failed") from None



def collect_project_state(read, namespaces):
    """Fingerprint native product parents/stock and require the complete owned flow.

    ``read`` accepts only named fixed scopes and bound parameters. This keeps the
    ownership/projection contract independently testable without emulating SQL.
    """
    named = {ns: {row["name"]: row["id"] for row in records} for ns, records in namespaces.items()}
    m1 = named["portfolio_trade_acceptance"]
    bridge = named["portfolio_order_bridge_acceptance"]
    flow = named["portfolio_order_bridge_flow"]
    bridge_products = [bridge[f"product_item{i}"] for i in (1, 2)]
    product_ids = sorted([m1[f"product_{key}"] for key in ("bearing", "sensor", "bolt")] + bridge_products)
    products = read("products", [product_ids])
    if len(products) != 5 or {row["id"] for row in products} != set(product_ids):
        raise ReleaseError("invalid_source")
    template_ids = sorted({row["product_tmpl_id"] for row in products})
    templates = read("templates", [template_ids])
    if (len(template_ids) != 5 or len(templates) != 5 or {row["id"] for row in templates} != set(template_ids)
            or not {bridge[f"template_item{i}"] for i in (1, 2)}.issubset(template_ids)):
        raise ReleaseError("invalid_source")
    sources = read("source", [bridge["source"]])
    if len(sources) != 1 or sources[0]["id"] != bridge["source"] or type(sources[0]["code"]) is not str:
        raise ReleaseError("invalid_source")
    jobs = read("owned_jobs", [bridge["source"]])
    attempts = read("owned_attempts", [bridge["source"]])
    quotes = read("owned_quotes", [bridge["customer"], sources[0]["code"] + ":%", bridge_products])
    expected_jobs = {flow[f"job_{suffix}"] for suffix in ("success", "repaired")}
    expected_attempts = {identifier for key, identifier in flow.items() if key.startswith("attempt_")}
    expected_quotes = {flow[f"quote_{suffix}"] for suffix in ("success", "repaired")}
    if (len(jobs) != 2 or {row["id"] for row in jobs} != expected_jobs
            or len(attempts) != 4 or {row["id"] for row in attempts} != expected_attempts
            or len(quotes) != 2 or {row["id"] for row in quotes} != expected_quotes):
        raise ReleaseError("invalid_source")
    return {"products": products, "product_templates": templates,
            **{scope: read(scope, [product_ids]) for scope in ("stock_move", "stock_move_line", "stock_quant")},
            "owned_jobs": jobs, "owned_attempts": attempts, "owned_quotes": quotes}


def _project_rows(cursor, scope, parameters):
    """Translate fixed private scopes to SELECTs; no caller supplies table names."""
    queries = {
        "products": "SELECT to_jsonb(t) FROM product_product t WHERE id=ANY(%s) ORDER BY id",
        "templates": "SELECT to_jsonb(t) FROM product_template t WHERE id=ANY(%s) ORDER BY id",
        "source": "SELECT to_jsonb(t) FROM portfolio_import_source t WHERE id=%s ORDER BY id",
        "stock_move": "SELECT to_jsonb(t) FROM stock_move t WHERE product_id=ANY(%s) ORDER BY id",
        "stock_move_line": "SELECT to_jsonb(t) FROM stock_move_line t WHERE product_id=ANY(%s) ORDER BY id",
        "stock_quant": "SELECT to_jsonb(t) FROM stock_quant t WHERE product_id=ANY(%s) ORDER BY id",
        "owned_jobs": "SELECT to_jsonb(t) FROM portfolio_import_job t WHERE source_id=%s ORDER BY id",
        "owned_attempts": "SELECT to_jsonb(a) FROM portfolio_import_attempt a JOIN portfolio_import_job j "
                          "ON j.id=a.job_id WHERE j.source_id=%s ORDER BY a.id",
        "owned_quotes": "SELECT to_jsonb(s) FROM sale_order s WHERE partner_id=%s OR origin LIKE %s OR id IN "
                        "(SELECT order_id FROM sale_order_line WHERE product_id=ANY(%s)) ORDER BY id",
    }
    if scope not in queries:
        raise ReleaseError("invalid_source")
    cursor.execute(queries[scope], parameters)
    return [row[0] for row in cursor.fetchall()]


def source_snapshot(config, database=None, purpose=None):
    """Canonical read-only business fingerprint, independent of connection counts.

    Clone callers supply an already generated target and its purpose. The returned
    database identity remains the source identity so business comparison is exact.
    Native attachment reads are a separate verification step after restoration.
    """
    connection = connect_readonly(config, database, purpose)
    try:
        with connection.cursor() as cursor:
            def rows(query, parameters=()):
                cursor.execute(query, parameters)
                return cursor.fetchall()

            namespaces = {}
            by_model = {}
            for namespace, expected in IDENTITIES.items():
                records = rows("SELECT name,model,res_id FROM ir_model_data WHERE module=%s ORDER BY name", [namespace])
                if [(name, model) for name, model, _ in records] != sorted(expected.items()):
                    raise ReleaseError("invalid_source")
                namespaces[namespace] = [{"name": n, "model": m, "id": i} for n, m, i in records]
                for _, model, identifier in records:
                    by_model.setdefault(model, set()).add(identifier)
            named = {ns: {row["name"]: row["id"] for row in data} for ns, data in namespaces.items()}
            m1, flow = named["portfolio_trade_acceptance"], named["portfolio_trade_fulfillment"]
            bridge, jobs = named["portfolio_order_bridge_acceptance"], named["portfolio_order_bridge_flow"]
            module_rows = rows("SELECT name,state,latest_version FROM ir_module_module WHERE name=ANY(%s) ORDER BY name",
                               [sorted(MODULE_VERSIONS)])
            if {name: version for name, state, version in module_rows if state == "installed"} != MODULE_VERSIONS:
                raise ReleaseError("invalid_source")
            company, bearing = m1["company_a"], m1["product_bearing"]
            stock = rows("SELECT COALESCE(SUM(q.quantity),0) FROM stock_quant q JOIN stock_location l ON l.id=q.location_id "
                         "WHERE q.product_id=%s AND q.company_id=%s AND l.usage IN ('internal','transit')", [bearing, company])[0][0]
            delivered, returned = rows(
                "SELECT COALESCE(SUM(CASE WHEN dst.usage='customer' AND src.usage!='customer' THEN m.quantity ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN src.usage='customer' AND dst.usage!='customer' THEN m.quantity ELSE 0 END),0) "
                "FROM stock_move m JOIN stock_location src ON src.id=m.location_id JOIN stock_location dst ON dst.id=m.location_dest_id "
                "JOIN sale_order_line sol ON sol.id=m.sale_line_id WHERE sol.order_id=%s AND m.state='done' AND m.company_id=%s",
                [flow["sale_order"], company])[0]
            net_amount = rows("SELECT COALESCE(SUM(amount_total_signed),0) FROM account_move "
                              "WHERE id=ANY(%s) AND state='posted' AND company_id=%s",
                              [[flow[key] for key in ("invoice_first", "invoice_second", "credit_note")], company])[0][0]
            golden = dict(zip(GOLDEN, map(float, (stock, delivered, returned, delivered - returned, net_amount)), strict=True))
            if golden != GOLDEN:
                raise ReleaseError("invalid_source")
            role_ids = sorted(by_model["res.users"])
            if len(role_ids) != 10:
                raise ReleaseError("invalid_source")
            roles = rows("SELECT to_jsonb(u),to_jsonb(p) FROM res_users u JOIN res_partner p ON p.id=u.partner_id "
                         "WHERE u.id=ANY(%s) ORDER BY u.id", [role_ids])
            groups = rows("SELECT uid,gid FROM res_groups_users_rel WHERE uid=ANY(%s) ORDER BY uid,gid", [role_ids])
            companies = rows("SELECT user_id,cid FROM res_company_users_rel WHERE user_id=ANY(%s) ORDER BY user_id,cid", [role_ids])
            if len(roles) != 10:
                raise ReleaseError("invalid_source")
            policies = rows("SELECT to_jsonb(c) FROM res_company c WHERE id=ANY(%s) ORDER BY id",
                            [[m1["company_a"], m1["company_b"]]])
            job_rows = rows("SELECT id,state,submitted_by_id,sale_order_id FROM portfolio_import_job WHERE id=ANY(%s) ORDER BY id",
                            [[jobs["job_success"], jobs["job_repaired"]]])
            expected_quotes = {jobs["quote_success"]: 309.0, jobs["quote_repaired"]: 161.5}
            quotes = rows("SELECT s.id,s.state,s.user_id,s.amount_total,c.name FROM sale_order s "
                          "JOIN res_currency c ON c.id=s.currency_id WHERE s.id=ANY(%s) ORDER BY s.id", [sorted(expected_quotes)])
            attempts = rows("SELECT id,actor_id FROM portfolio_import_attempt WHERE job_id=ANY(%s) ORDER BY id",
                            [[jobs["job_success"], jobs["job_repaired"]]])
            if (len(job_rows) != 2 or any(state != "done" or uid != bridge["user_integration"] or quote not in expected_quotes
                                         for _, state, uid, quote in job_rows)
                    or len(quotes) != 2 or any(state != "draft" or uid != m1["user_sales"] or float(amount) != expected_quotes[qid]
                                               or currency != "CNY" for qid, state, uid, amount, currency in quotes)
                    or len(attempts) != 4 or any(uid != bridge["user_operator"] for _, uid in attempts)):
                raise ReleaseError("invalid_source")
            business = {"project_state": collect_project_state(
                lambda scope, parameters: _project_rows(cursor, scope, parameters), namespaces)}
            finance = rows("SELECT name,model,res_id FROM ir_model_data WHERE module=%s ORDER BY name",
                           ["portfolio_trade_finance"])
            expected_finance = {
                **dict.fromkeys(("account_income", "account_expense", "account_receivable", "account_payable"), "account.account"),
                "journal_sale": "account.journal", "pricelist_cny": "product.pricelist",
            }
            if [(name, model) for name, model, _ in finance] != sorted(expected_finance.items()):
                raise ReleaseError("invalid_source")
            business["finance_identities"] = finance
            for _, model, identifier in finance:
                by_model.setdefault(model, set()).add(identifier)
            # Identifiers and SQL table names come only from these fixed contracts.
            for model, identifiers in sorted(by_model.items()):
                if model in {"res.users", "res.company"}:
                    continue
                table = model.replace(".", "_")
                if model == "ir.actions.server":
                    table = "ir_act_server"
                values = rows(f"SELECT to_jsonb(t) FROM {table} t WHERE id=ANY(%s) ORDER BY id", [sorted(identifiers)])
                if len(values) != len(identifiers):
                    raise ReleaseError("invalid_source")
                business[model] = values
            for table, foreign_key, identifiers in (
                ("sale_order_line", "order_id", by_model["sale.order"]),
                ("purchase_order_line", "order_id", by_model["purchase.order"]),
                ("stock_move", "picking_id", by_model["stock.picking"]),
                ("stock_move_line", "picking_id", by_model["stock.picking"]),
                ("account_move_line", "move_id", by_model["account.move"]),
            ):
                business[table] = rows(f"SELECT to_jsonb(t) FROM {table} t WHERE {foreign_key}=ANY(%s) ORDER BY id", [sorted(identifiers)])
            business["source_memberships"] = rows(
                "SELECT to_jsonb(t) FROM portfolio_import_source_res_users_rel t ORDER BY 1")
            attachments = []
            attachment_facts = []
            for identifier, kind, filename, checksum, size, data, url, metadata in rows(
                    "SELECT id,type,store_fname,checksum,file_size,db_datas,url,to_jsonb(a)-'db_datas'-'url' "
                    "FROM ir_attachment a ORDER BY id"):
                db_hash = hashlib.sha256(bytes(data)).hexdigest() if data is not None else None
                url_hash = hashlib.sha256(url.encode()).hexdigest() if url is not None else None
                attachments.append({"id": identifier, "type": kind, "store_fname": filename, "checksum": checksum,
                                    "file_size": size or 0, "db_sha256": db_hash, "url_sha256": url_hash})
                attachment_facts.append((metadata, db_hash, url_hash))
            result = {"schema_version": 1, "database": DATABASE, "namespaces": namespaces,
                      "module_versions": MODULE_VERSIONS.copy(), "golden": golden,
                      "role_fingerprint": digest([roles, groups, companies]), "policy_fingerprint": digest(policies),
                      "business_fingerprint": digest(business), "attachment_fingerprint": digest(attachment_facts),
                      "attachments": attachments}
            validate_snapshot(result)
            return result
    except ReleaseError:
        raise
    except Exception:
        raise ReleaseError("database_failed") from None
    finally:
        connection.rollback()
        connection.close()
