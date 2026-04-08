@app.route("/admin")
@login_required
def admin():
    search = clean_text(request.args.get("search"), 100)
    status = clean_text(request.args.get("status"), 20)
    priority = clean_text(request.args.get("priority"), 20)
    broken_only = request.args.get("broken_only", "").strip()
    fix_join_only = request.args.get("fix_join_only", "").strip()
    marketing_only = request.args.get("marketing_only", "").strip()
    payment_status = clean_text(request.args.get("payment_status"), 20)

    query = "SELECT * FROM signups WHERE 1=1"
    params = []

    if search:
        query += """
            AND (
                full_name ILIKE %s OR
                email ILIKE %s OR
                phone ILIKE %s OR
                postcode ILIKE %s
            )
        """
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term, search_term])

    if status in VALID_STATUSES:
        query += " AND status = %s"
        params.append(status)

    if priority in {"HIGH", "NORMAL"}:
        query += " AND priority = %s"
        params.append(priority)

    if payment_status in VALID_PAYMENT_STATUSES:
        query += " AND payment_status = %s"
        params.append(payment_status)

    if broken_only == "1":
        query += " AND boiler_broken = 'Yes'"

    if fix_join_only == "1":
        query += " AND fix_and_join = 'Yes'"

    if marketing_only == "1":
        query += " AND marketing_opt_in = 1"

    query += " ORDER BY CASE WHEN priority = 'HIGH' THEN 0 ELSE 1 END, id DESC"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            signups = cur.fetchall()

            cur.execute("SELECT COUNT(*) AS count FROM signups")
            total_signups = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE payment_status = 'Paid'")
            total_paid = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE payment_status IN ('Not sent', 'Link sent', 'Failed', 'Refunded')")
            total_unpaid = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE payment_status = 'Failed'")
            total_failed = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE priority = 'HIGH'")
            total_high_priority = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE boiler_broken = 'Yes'")
            total_broken = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE fix_and_join = 'Yes'")
            total_fix_join = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE selected_plan = 'Essential'")
            total_essential = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE selected_plan = 'Standard'")
            total_standard = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) AS count FROM signups WHERE selected_plan = 'Complete'")
            total_complete = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM signups
                WHERE created_at >= date_trunc('day', NOW())
                """
            )
            signups_today = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM signups
                WHERE created_at >= date_trunc('week', NOW())
                """
            )
            signups_this_week = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM signups
                WHERE created_at >= date_trunc('month', NOW())
                """
            )
            signups_this_month = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COALESCE(SUM(CAST(monthly_price AS numeric)), 0) AS total
                FROM signups
                WHERE payment_status = 'Paid'
                """
            )
            monthly_revenue = cur.fetchone()["total"]

    finally:
        conn.close()

    conversion_rate = 0
    if total_signups > 0:
        conversion_rate = round((total_paid / total_signups) * 100, 1)

    stats = {
        "total_signups": total_signups,
        "total_paid": total_paid,
        "total_unpaid": total_unpaid,
        "total_failed": total_failed,
        "total_high_priority": total_high_priority,
        "total_broken": total_broken,
        "total_fix_join": total_fix_join,
        "total_essential": total_essential,
        "total_standard": total_standard,
        "total_complete": total_complete,
        "signups_today": signups_today,
        "signups_this_week": signups_this_week,
        "signups_this_month": signups_this_month,
        "monthly_revenue": monthly_revenue,
        "conversion_rate": conversion_rate,
    }

    return render_template(
        "admin.html",
        signups=signups,
        stats=stats,
        filters={
            "search": search,
            "status": status,
            "priority": priority,
            "payment_status": payment_status,
            "broken_only": broken_only,
            "fix_join_only": fix_join_only,
            "marketing_only": marketing_only,
        },
        valid_statuses=sorted(VALID_STATUSES),
        valid_payment_statuses=sorted(VALID_PAYMENT_STATUSES),
    )