---
name: kb-article-from-ticket
description: 把多份已解决工单整理为知识文章草稿，人工审核后发布。
---

# kb-article-from-ticket

仅服务台使用。先search_resolved_tickets，至少两张verified工单佐证同一处理办法；排除原因不明的记录。删除人名、手机号。用draft_article保存标题、正文、source_ticket_ids，并展示草稿和分歧。用户明确审核同意后才调用publish_article且human_approved=true。发布后提醒同步知识库，不能把草稿当成已发布文章。
