-- 三表循环外键：dept <-> employee <-> project
-- 原始库中所有外键均有效。下面的语句必然触发唯一约束错误。
INSERT INTO dept (id, name) VALUES (1, 'Platform');
