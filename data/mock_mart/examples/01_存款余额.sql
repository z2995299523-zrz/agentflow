-- 对公存款余额类样例
-- 专题层的存款汇总表有两张：按客户和机构汇总的 ads.corp_cust_dep_bal_d，按机构汇总的 ads.org_corp_dep_bal_d。
-- 三个存款余额字段要分清：dep_bal_rmb 不含保证金，margin_bal_rmb 是保证金，dep_bal_tot_rmb 是两者之和。

-- 需求: 2026 年 3 月末活期存款、定期存款按机构汇总，带上机构名称，活期多的排前面
-- 口径: 活期、定期存款余额只有客户级汇总表里有，要先按机构汇总，再关联机构维表取名称；两者都不含保证金
SELECT b.org_no,
       o.org_nm,
       SUM(b.cur_dep_bal_rmb) AS cur_dep_bal_rmb,
       SUM(b.fix_dep_bal_rmb) AS fix_dep_bal_rmb
FROM ads.corp_cust_dep_bal_d b
JOIN dim.org o ON b.org_no = o.org_no
WHERE b.data_dt = DATE '2026-03-31'
GROUP BY b.org_no, o.org_nm
ORDER BY cur_dep_bal_rmb DESC;

-- 需求: 2026 年 6 月末各二级分行含保证金存款比年初多了多少，增加额大的排前面
-- 口径: 含保证金用全口径存款余额 dep_bal_tot_rmb；年初指上年末（2025-12-31）。本期和年初两天的余额一次取出，用 CASE 分开后相减
SELECT o.lvl2_org_nm,
       SUM(CASE WHEN b.data_dt = DATE '2026-06-30' THEN b.dep_bal_tot_rmb ELSE 0 END)
     - SUM(CASE WHEN b.data_dt = DATE '2025-12-31' THEN b.dep_bal_tot_rmb ELSE 0 END) AS dep_bal_tot_inc
FROM ads.org_corp_dep_bal_d b
JOIN dim.org o ON b.org_no = o.org_no
WHERE b.data_dt IN (DATE '2026-06-30', DATE '2025-12-31')
GROUP BY o.lvl2_org_nm
ORDER BY dep_bal_tot_inc DESC;

-- 需求: 统计 2026 年 8 月末各二级分行的存款月日均
-- 口径: 不含保证金。月日均字段 mtd_avg_bal_rmb 已经是月初到数据日期的累计日均，只取 8 月 31 日这一天的值，不能再跨日期求平均；各客户、各机构的日均可以直接相加
SELECT o.lvl2_org_nm,
       SUM(b.mtd_avg_bal_rmb) AS mtd_avg_bal_rmb
FROM ads.corp_cust_dep_bal_d b
JOIN dim.org o ON b.org_no = o.org_no
WHERE b.data_dt = DATE '2026-08-31'
GROUP BY o.lvl2_org_nm;

-- 需求: 2026 年 5 月末全行存款年日均与时点余额的对比，年日均相当于时点余额的百分之几，精确到小数点后两位
-- 口径: 不含保证金。年日均 ytd_avg_bal_rmb 是年初到数据日期的累计日均，时点余额用 dep_bal_rmb，两者都只取 5 月 31 日这一天，机构之间可以直接相加；比值要先汇总再相除
SELECT SUM(ytd_avg_bal_rmb) AS ytd_avg_bal_rmb,
       SUM(dep_bal_rmb) AS dep_bal_rmb,
       ROUND(100.0 * SUM(ytd_avg_bal_rmb) / NULLIF(SUM(dep_bal_rmb), 0), 2) AS avg_to_bal_pct
FROM ads.org_corp_dep_bal_d
WHERE data_dt = DATE '2026-05-31';

-- 需求: 2026 年 7 月末每个二级分行有多少户有存款的企业客户，跨支行开户的客户只数一次
-- 口径: 有存款指全口径存款余额大于零。客户数不能直接相加，同一客户可能在多个机构开户，要用 COUNT(DISTINCT cust_no)，不能把机构汇总表的有余额客户数 dep_cust_cnt 加起来
SELECT o.lvl2_org_nm,
       COUNT(DISTINCT b.cust_no) AS cust_cnt
FROM ads.corp_cust_dep_bal_d b
JOIN dim.org o ON b.org_no = o.org_no
WHERE b.data_dt = DATE '2026-07-31'
  AND b.dep_bal_tot_rmb > 0
GROUP BY o.lvl2_org_nm;

-- 需求: 2026 年 5 月末一级分行的保证金存款占比，百分数精确到小数点后两位
-- 口径: 占比 = 保证金存款余额 margin_bal_rmb ÷ 全口径存款余额 dep_bal_tot_rmb；先按一级分行汇总，再相除，不要对各机构的比例求平均
SELECT o.lvl1_org_nm,
       ROUND(100.0 * SUM(b.margin_bal_rmb) / NULLIF(SUM(b.dep_bal_tot_rmb), 0), 2) AS margin_ratio_pct
FROM ads.org_corp_dep_bal_d b
JOIN dim.org o ON b.org_no = o.org_no
WHERE b.data_dt = DATE '2026-05-31'
GROUP BY o.lvl1_org_nm;
