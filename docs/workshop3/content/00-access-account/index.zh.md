---
title: "00 获取 Workshop Studio 实验账号"
weight: 5
---

# 第 00 章 · 获取 Workshop Studio 实验账号

> **目标**：登录 Workshop Studio，加入本次活动。
>
> **前置条件**：讲师提供的活动链接或 Event access code；一个能收邮件的邮箱；
> 一个现代浏览器（Chrome / Edge / Safari）。
>
> **预计耗时**：约 5 分钟。

---

> **Self-paced 路径**：本章和第 01 章都只适用于 Workshop Studio 活动。使用自有 AWS
> 账号和本地开发机的参与者跳过这两章，直接看
> [可选第 02 章 · Self-paced 自有 AWS 账号](../02-own-account-local)。

Workshop Studio 会为每位参与者分配一个临时 AWS 账号。账号在活动结束时自动回收，
不产生个人费用。本实验的全部操作都在这个账号里完成。

## 0.1 登录 Workshop Studio

1. 打开讲师提供的活动链接；没有专属链接时打开
   [Workshop Studio](https://catalog.workshops.aws/)。

2. 点击 **Email one-time password (OTP)**。

   ![Workshop Studio 登录页](/static/images/00-ws-signin.png)
   *图 0-1：登录方式选择页，选择邮箱一次性密码。*

3. 输入邮箱地址，点击 **Send passcode**。

   ![输入邮箱](/static/images/00-ws-send-passcode.png)
   *图 0-2：输入邮箱后发送验证码。*

4. 到邮箱里找到验证码邮件，把 9 位验证码填入页面，点击 **Sign in**。

   ![输入验证码](/static/images/00-ws-email-passcode.png)
   *图 0-3：粘贴邮件里的一次性验证码完成登录。*

> **没收到邮件**：先查垃圾邮件文件夹；仍然没有时，请讲师确认这个邮箱在活动白名单里。
> 白名单按邮箱精确匹配，换一个邮箱地址需要讲师重新加入。

## 0.2 加入本次活动

1. 输入讲师提供的 **Event access code**，点击 **Next**。使用的链接里已带 event code
   时没有这一步。

   ![输入 Event Code](/static/images/00-ws-event-code.png)
   *图 0-4：输入活动接入码。*

2. 阅读 Terms and Conditions，勾选 **I agree with the Terms and Conditions**，
   点击 **Join event**。

加入后进入活动页面：左侧是实验章节列表，顶部显示活动剩余时间。

![活动页面](/static/images/00-ws-temp-account.png)
*图 0-5：加入活动后的页面，临时账号已就绪，顶部可见剩余时间。*

控制台登录凭据（`ConsoleUrl` / `ConsoleUsername` / `ConsolePassword`）的获取和使用在
[第 01 章](../01-environment)。

## 本章验证清单

- [ ] 已用邮箱 OTP 登录 Workshop Studio
- [ ] 已加入本次活动，左侧能看到实验章节列表
- [ ] 顶部能看到活动剩余时间

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 验证码邮件一直不来 | 邮件进了垃圾箱，或邮箱不在活动白名单 | 先查垃圾箱；仍没有则请讲师核对白名单里的邮箱地址 |
| Event access code 无效 | 输错、活动未开始或已结束 | 与讲师核对 code 和活动状态 |

---

下一章：[第 01 章 · 实验环境准备与控制台导览](../01-environment)
