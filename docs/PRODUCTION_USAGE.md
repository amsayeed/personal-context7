# Production Deployment Usage Guide

This guide explains how to use the **Personal Context7** production deployment with your AI coding agents via MCP (Model Context Protocol).

## 🚀 Production Deployment Details

- **Service URL**: `https://personal-context7-production.up.railway.app`
- **Status**: ✅ Live and operational
- **Transport**: SSE (Server-Sent Events) for MCP
- **Authentication**: Bearer token required

## 🔑 Getting Your API Key

### Option 1: Via Railway CLI (Recommended)

If you have the Railway CLI installed:

```bash
railway variable list --service personal-context7
```

Look for the `PKB_API_KEY` variable value.

### Option 2: Via Railway Dashboard

1. Go to [Railway Dashboard](https://railway.com/project/13830219-e3d6-4e90-9171-0e52b28da002)
2. Select the `personal-context7` service
3. Click on "Variables" tab
4. Copy the value of `PKB_API_KEY`

### Option 3: Set a New API Key

If you want to set your own API key:

```bash
railway variable set PKB_API_KEY=your-secure-api-key-here --service personal-context7
railway restart --service personal-context7 --yes
```

## 🔌 Connecting AI Coding Agents

### Claude Code

Add this to your `~/.config/claude-code/mcp_servers.json`:

```json
{
  "mcpServers": {
    "pkb": {
      "type": "sse",
      "url": "https://personal-context7-production.up.railway.app/sse",
      "headers": {
        "Authorization": "Bearer YOUR_PKB_API_KEY"
      }
    }
  }
}
```

Replace `YOUR_PKB_API_KEY` with your actual API key.

**Restart Claude Code** to load the new MCP server.

### Claude Desktop (Cowork)

1. Open Claude Desktop
2. Go to **Settings** → **Extensions** → **MCP Servers**
3. Click **Add Custom Server**
4. Configure as follows:
   - **Name**: `pkb`
   - **Type**: `SSE`
   - **URL**: `https://personal-context7-production.up.railway.app/sse`
   - **Headers**: 
     ```
     Authorization: Bearer YOUR_PKB_API_KEY
     ```
5. Click **Add** and restart Claude Desktop

### Cursor

Add this to your `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "pkb": {
      "url": "https://personal-context7-production.up.railway.app/sse",
      "headers": {
        "Authorization": "Bearer YOUR_PKB_API_KEY"
      }
    }
  }
}
```

### Continue / Windsurf

Add to your respective MCP configuration file (typically `~/.config/continue/mcp_servers.json` or similar):

```json
{
  "mcpServers": {
    "pkb": {
      "transport": "sse",
      "url": "https://personal-context7-production.up.railway.app/sse",
      "headers": {
        "Authorization": "Bearer YOUR_PKB_API_KEY"
      }
    }
  }
}
```

## 🧪 Testing the Connection

### Health Check (No Auth Required)

```bash
curl https://personal-context7-production.up.railway.app/healthz
# Expected response: {"ok":true}
```

### Stats Check (Auth Required)

```bash
curl -H "Authorization: Bearer YOUR_PKB_API_KEY" \
  https://personal-context7-production.up.railway.app/stats
```

This will show you the current state of your knowledge base including:
- Document count
- Chunk count  
- Embedding model info
- Database size
- Last sync status

### Trigger Manual Sync (Auth Required)

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_PKB_API_KEY" \
  https://personal-context7-production.up.railway.app/webhook/sync
```

## 🛠️ Available MCP Tools

Once connected, your AI agent will have access to these tools:

| Tool | Purpose |
|------|---------|
| `resolve_topic` | Find which documents cover a specific topic |
| `get_docs` | Retrieve ranked chunks from a specific document |
| `search` | One-shot hybrid search (semantic + keyword) |
| `smart_search` | Expanded search with query variants |
| `multi_search` | Search multiple queries at once |
| `hyde_search` | Hypothetical document embedding search |
| `doctor_json` | KB hygiene and metadata report |
| `sync` | Trigger git pull + reindex |
| `stats` | Get index statistics |

## 📝 Example Usage with Claude Code

Once configured, you can ask Claude Code to use your knowledge base:

```
> Using pkb, search for notes about database indexing strategies
> What does my knowledge base say about microservices architecture?
> Find all my notes related to Python performance optimization
> Using pkb, compare my notes on Redis vs Memcached
```

## 🔄 Keeping the Knowledge Base Fresh

### Setting Up Git Repository

To make your knowledge base functional, you need to configure it to pull from a git repository:

1. **Set the Git Remote URL** in Railway variables:
   ```bash
   railway variable set PKB_KB_GIT_REMOTE=https://github.com/yourusername/your-notes-repo.git --service personal-context7
   railway restart --service personal-context7 --yes
   ```

2. **Trigger Initial Sync**:
   ```bash
   curl -X POST \
     -H "Authorization: Bearer YOUR_PKB_API_KEY" \
     https://personal-context7-production.up.railway.app/webhook/sync
   ```

### Automated Sync via GitHub Action

Add this workflow to your **notes repository** (`.github/workflows/sync-pkb.yml`):

```yaml
name: Sync Personal Knowledge Base
on:
  push:
    branches: [main]
  workflow_dispatch: {}

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger PKB Sync
        run: |
          curl -fsSL -X POST \
            -H "Authorization: Bearer ${{ secrets.PKB_API_KEY }}" \
            "${{ secrets.PKB_URL }}/webhook/sync"
```

**Add these secrets to your notes repository:**
- `PKB_API_KEY`: Your Railway API key
- `PKB_URL`: `https://personal-context7-production.up.railway.app`

Now every push to your notes repository will automatically update the knowledge base.

## 🐛 Troubleshooting

### Connection Issues

1. **Check service status**:
   ```bash
   railway status --service personal-context7
   ```

2. **View logs**:
   ```bash
   railway logs --service personal-context7
   ```

3. **Verify API key**:
   ```bash
   curl -H "Authorization: Bearer YOUR_PKB_API_KEY" \
     https://personal-context7-production.up.railway.app/stats
   ```

### Empty Knowledge Base

If `/stats` shows `documents: 0`:

1. Verify `PKB_KB_GIT_REMOTE` is set correctly
2. Check the git repository is accessible
3. Trigger a manual sync and check logs for errors

### MCP Server Not Showing in Agent

1. Verify the URL is correct: `https://personal-context7-production.up.railway.app/sse`
2. Check the API key format: `Authorization: Bearer YOUR_KEY`
3. Restart the AI agent application
4. Check agent logs for MCP connection errors

## 🔒 Security Best Practices

- **Never commit API keys** to repositories
- **Rotate API keys** periodically via Railway dashboard
- **Use GitHub Secrets** for storing keys in workflows
- **Monitor logs** for unusual activity
- **Keep API keys** secure and share only with trusted services

## 📚 Additional Resources

- **Main Documentation**: See other docs in this directory for detailed setup
- **Railway Dashboard**: https://railway.com/project/13830219-e3d6-4e90-9171-0e52b28da002
- **GitHub Repository**: https://github.com/amsayeed/personal-context7
- **MCP Protocol**: https://modelcontextprotocol.io/

## 🆘 Support

If you encounter issues:

1. Check Railway service logs
2. Verify environment variables are set correctly
3. Test API endpoints manually with curl
4. Review the main documentation in this repository

---

**Last Updated**: 2025-05-20  
**Deployment Version**: Production  
**Service Status**: ✅ Operational