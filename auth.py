"""角色与访问控制。

四类角色：
* ``admin``     —— 研究管理员，拥有全部裁决权
* ``researcher`` —— 农技/研究负责人：冻结方案、登记作业、裁决异常、重建产量、轨迹反查
* ``lab``       —— 实验室：样本转交后的重分装/耗用/作废（沿用原始谱系）
* ``operator``  —— 普通机手：只能上报与查看自己当前作业所需的信息

令牌为 256 位随机 Bearer Key，用户表保存在数据目录 users.json（权限 600）。
首次启动自动播种四个演示用户并打印一次密钥；生产环境通过预置 users.json 覆盖。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets

ROLES = ("admin", "researcher", "lab", "operator")


class AuthError(Exception):
    def __init__(self, code, message, status=401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Auth:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "users.json")
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.users = json.load(fh)
        else:
            self.users = self._seed()

    def _seed(self):
        seeds = [
            ("u_admin", "研究管理员", "admin"),
            ("u_lead", "研究负责人", "researcher"),
            ("u_lab", "中心实验室", "lab"),
            ("u_driver", "示范机手", "operator"),
        ]
        users = {}
        lines = ["# 首次启动生成的演示密钥（生产环境请预置 users.json 并删除本提示）"]
        for user_id, name, role in seeds:
            key = secrets.token_urlsafe(32)
            users[key] = {"user_id": user_id, "name": name, "role": role}
            lines.append(f"{role:10s} {user_id:9s} {key}")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(users, fh, ensure_ascii=False, indent=2)
        os.chmod(self.path, 0o600)
        print("\n".join(lines))
        return users

    def authenticate(self, authorization_header):
        if not authorization_header:
            raise AuthError("missing_token", "缺少 Authorization: Bearer <token>")
        parts = authorization_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise AuthError("malformed_token", "令牌格式应为 Bearer <token>")
        token = parts[1].strip()
        # 恒定时间比较，避免通过响应时序枚举密钥
        matched = None
        for key, user in self.users.items():
            if hmac.compare_digest(key, token):
                matched = dict(user)
        if matched is None:
            raise AuthError("invalid_token", "令牌无效或已吊销", 401)
        return matched


def require_role(actor, roles):
    if actor["role"] not in roles:
        raise AuthError(
            "forbidden",
            f"当前角色 {actor['role']} 无权执行该操作，允许角色: {', '.join(roles)}",
            403,
        )
