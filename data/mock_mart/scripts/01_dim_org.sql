-- 机构维表：把机构的上下级关系拉平，每个机构带上所属的一级分行和二级分行
-- 来源：ods.org_info（自关联两次，分别取上级和上上级）
-- 全量刷新

TRUNCATE TABLE dim.org;

INSERT INTO dim.org (org_no, org_nm, org_lvl, lvl1_org_no, lvl1_org_nm, lvl2_org_no, lvl2_org_nm)
SELECT o.org_no,
       o.org_nm,
       o.org_lvl,
       CASE o.org_lvl WHEN '2' THEN o.org_no WHEN '3' THEN p.org_no WHEN '4' THEN g.org_no END AS lvl1_org_no,
       CASE o.org_lvl WHEN '2' THEN o.org_nm WHEN '3' THEN p.org_nm WHEN '4' THEN g.org_nm END AS lvl1_org_nm,
       CASE o.org_lvl WHEN '3' THEN o.org_no WHEN '4' THEN p.org_no END AS lvl2_org_no,
       CASE o.org_lvl WHEN '3' THEN o.org_nm WHEN '4' THEN p.org_nm END AS lvl2_org_nm
FROM ods.org_info o
LEFT JOIN ods.org_info p ON o.up_org_no = p.org_no
LEFT JOIN ods.org_info g ON p.up_org_no = g.org_no;
