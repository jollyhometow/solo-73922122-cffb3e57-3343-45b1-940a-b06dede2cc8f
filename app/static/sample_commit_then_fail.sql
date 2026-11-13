-- 先提交修改，再触发 NOT NULL 约束错误。
-- 删除的是叶子节点（无子节点引用），COMMIT 必然成功；
-- 随后的 INSERT 因 label 为 NULL 失败。
-- 每个试跑使用独立副本，已提交的 DELETE 不会污染原件或后续试跑。
BEGIN;
DELETE FROM node WHERE id = 3;
COMMIT;
INSERT INTO node (id, parent_id, label) VALUES (9, 1, NULL);
