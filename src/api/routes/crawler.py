"""
爬虫路由
处理爬虫相关的 API 请求
"""
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import traceback

from src.api.deps import get_db
from src.services.crawler_service import CrawlerService

router = APIRouter(prefix="/crawler", tags=["爬虫"])


class WeChatArticleRequest(BaseModel):
    url: str
    post_date: Optional[str] = None


class EastmoneyBlogRequest(BaseModel):
    max_articles: int = 15
    concurrent: bool = True
    max_workers: int = 5


class SinaFinanceRequest(BaseModel):
    category: str = 'finance'
    max_articles: int = 20
    concurrent: bool = True
    max_workers: int = 5


class EastmoneyGuideRequest(BaseModel):
    max_articles: int = 20
    concurrent: bool = True
    max_workers: int = 5


class SinaBlogRequest(BaseModel):
    max_posts: int = 20
    concurrent: bool = True
    max_workers: int = 5


@router.get("/status")
async def get_crawler_status(db: Session = Depends(get_db)):
    """获取爬虫模块状态"""
    service = CrawlerService(db)
    return {
        "success": True,
        "data": service.get_crawler_status()
    }


@router.post("/eastmoney-blog/auto-adopt")
async def auto_adopt_eastmoney_blog(data: EastmoneyBlogRequest, db: Session = Depends(get_db)):
    """抓取东方财富博客并自动采纳符合标准的文章"""
    service = CrawlerService(db)

    try:
        result = service.crawl_eastmoney_blog(
            max_articles=data.max_articles,
            concurrent=data.concurrent,
            max_workers=min(data.max_workers, 5)
        )
        return result
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "message": f"自动采纳失败: {e}"}


@router.post("/eastmoney-guide/auto-adopt")
async def auto_adopt_eastmoney_guide(data: EastmoneyGuideRequest, db: Session = Depends(get_db)):
    """抓取东财博客导读并自动采纳符合标准的文章"""
    service = CrawlerService(db)

    try:
        result = service.crawl_eastmoney_guide(
            max_articles=data.max_articles,
            concurrent=data.concurrent,
            max_workers=min(data.max_workers, 5)
        )
        return result
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "message": f"自动采纳失败: {e}"}


@router.post("/sina-finance/auto-adopt")
async def auto_adopt_sina_finance(data: SinaFinanceRequest, db: Session = Depends(get_db)):
    """抓取新浪财经并自动采纳符合标准的文章"""
    service = CrawlerService(db)

    try:
        result = service.crawl_sina_finance(
            category=data.category,
            max_articles=data.max_articles,
            concurrent=data.concurrent,
            max_workers=min(data.max_workers, 5)
        )
        return result
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "message": f"自动采纳失败: {e}"}


@router.post("/sina-blog/auto-adopt")
async def auto_adopt_sina_blog(data: SinaBlogRequest, db: Session = Depends(get_db)):
    """抓取新浪博客并自动采纳符合标准的文章"""
    service = CrawlerService(db)

    try:
        result = service.crawl_sina_blog(
            max_posts=data.max_posts,
            concurrent=data.concurrent,
            max_workers=min(data.max_workers, 5)
        )
        return result
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "message": f"自动采纳失败: {e}"}


@router.post("/wechat/fetch")
async def fetch_wechat_article(data: WeChatArticleRequest, db: Session = Depends(get_db)):
    """
    抓取微信公众号文章并自动添加博主和帖子
    
    流程：
    1. 抓取文章内容
    2. 自动创建/匹配博主
    3. 创建帖子并分析
    """
    from src.crawler.wechat_fetcher import wechat_fetcher
    from src.models.database import Blogger, Post
    from src.services.post_service import PostService
    from src.analyzer.llm_analyzer import get_analyzer
    from datetime import datetime as dt
    
    try:
        article = await wechat_fetcher.fetch(data.url)
        
        if not article:
            return {"success": False, "message": "抓取失败：微信反爬拦截或URL无效。建议在浏览器打开文章后复制内容，通过「添加帖子」手动粘贴。"}
        
        author_name = article.get('author', '未知博主')
        
        blogger = db.query(Blogger).filter(Blogger.name == author_name).first()
        if not blogger:
            blogger = Blogger(
                name=author_name,
                platform='wechat',
                description=f'来自微信公众号'
            )
            db.add(blogger)
            db.commit()
            db.refresh(blogger)
        
        existing_post = db.query(Post).filter(Post.source_url == data.url).first()
        if existing_post:
            return {
                "success": True,
                "message": "该文章已存在",
                "data": {
                    "post_id": existing_post.id,
                    "title": existing_post.title,
                    "blogger_name": author_name,
                    "already_exists": True
                }
            }
        
        if data.post_date:
            try:
                post_date = dt.strptime(data.post_date, '%Y-%m-%d').date()
            except:
                post_date = dt.now().date()
        else:
            publish_time = article.get('publish_time', '')
            post_date = dt.now().date()
            if publish_time:
                try:
                    for fmt in ['%Y年%m月%d日', '%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            post_date = dt.strptime(publish_time[:10], fmt).date()
                            break
                        except:
                            continue
                except:
                    pass
        
        post_service = PostService(db)
        result = post_service.create_post_with_analysis(
            blogger_id=blogger.id,
            content=article['content'],
            post_date=post_date,
            source_url=data.url,
            async_mode=True
        )
        
        return {
            "success": True,
            "message": "文章抓取并分析成功",
            "data": {
                "post_id": result['id'],
                "title": article['title'],
                "blogger_name": author_name,
                "blogger_id": blogger.id,
                "analyzed": result['analyzed'],
                "predictions_created": result['predictions_created'],
                "already_exists": False
            }
        }
        
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "message": f"抓取失败: {str(e)}"}
