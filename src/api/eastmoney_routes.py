"""
东方财富博客爬虫 API 路由
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
from datetime import date

from src.models.database import SessionLocal, Viewpoint
from src.core.config import config

router = APIRouter(prefix="/api/crawler", tags=["东方财富博客爬虫"])

# 请求模型
class EastmoneyBlogRequest(BaseModel):
    max_articles: int = 15


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/eastmoney-blog")
def fetch_eastmoney_blog(data: EastmoneyBlogRequest, db: Session = Depends(get_db)):
    """抓取东方财富博客热门文章"""
    try:
        if not config.CRAWLER_ENABLED:
            return {
                "success": False,
                "message": "爬虫模块未启用，请在 .env 中设置 CRAWLER_ENABLED=true",
                "data": None
            }
        
        print(f"[Crawler API] 开始抓取东方财富博客热门文章...")
        
        # 导入东方财富博客爬虫
        from src.crawler.eastmoney_blog_crawler import crawler as eastmoney_crawler
        from src.crawler.enhanced_ai_analyzer import EnhancedAIAnalyzer
        ai_analyzer = EnhancedAIAnalyzer()
        
        # 抓取文章
        articles = eastmoney_crawler.fetch_hot_articles(max_articles=data.max_articles)
        
        all_articles = []
        for article in articles:
            # 使用AI分析文章
            try:
                ai_result = ai_analyzer.analyze_post({
                    'title': article['title'],
                    'content': article['content']
                })
                
                sentiment = ai_result.get('sentiment', 'neutral')
                sentiment_score = ai_result.get('sentiment_score', 0.0)
                sectors = ai_result.get('sectors', [])
                keywords = ai_result.get('keywords', [])
                ai_score = ai_result.get('score', 5.0)
                ai_category = ai_result.get('category', '其他')
                ai_reason = ai_result.get('reason', '')
            except Exception as e:
                print(f"[Crawler API] AI分析失败: {e}")
                sentiment = 'neutral'
                sentiment_score = 0.0
                sectors = []
                keywords = []
                ai_score = 5.0
                ai_category = '其他'
                ai_reason = ''
            
            all_articles.append({
                'article_id': article['article_id'],
                'title': article['title'],
                'content': article['content'][:500] if article['content'] else article['title'],
                'author': article['author'],
                'is_vip': article.get('is_vip', False),
                'publish_time': article.get('publish_time', ''),
                'quality_score': article.get('quality_score', 60),
                'url': article['url'],
                'crawl_time': article['crawl_time'],
                'source': 'eastmoney_blog',
                'sentiment': sentiment,
                'sentiment_score': sentiment_score,
                'sectors': sectors,
                'keywords': keywords,
                'ai_score': ai_score,
                'ai_category': ai_category,
                'ai_reason': ai_reason,
            })
        
        print(f"[Crawler API] 抓取完成，共 {len(all_articles)} 篇东方财富博客文章")
        
        return {
            "success": True,
            "message": f"成功抓取 {len(all_articles)} 篇东方财富博客文章",
            "data": {
                "total_articles": len(all_articles),
                "articles": all_articles
            }
        }
        
    except Exception as e:
        print(f"[Crawler API] 抓取东方财富博客文章失败：{e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "message": f"抓取失败：{e}",
            "data": None
        }


@router.post("/eastmoney-blog/adopt")
def adopt_eastmoney_article(data: dict, db: Session = Depends(get_db)):
    """将东方财富博客文章采纳为观点"""
    try:
        print(f"[Crawler API] 采纳东方财富博客文章: {data.get('title', '')[:30]}...")
        
        # 创建观点记录
        viewpoint = Viewpoint(
            viewpoint_date=date.today(),
            content=data.get('content', '')[:500],
            author=data.get('author', '未知'),
            market_direction=data.get('sentiment', 'neutral'),
            confidence=int(abs(data.get('sentiment_score', 0)) * 100),
            sectors_bullish=data.get('sectors', []) if data.get('sentiment') == 'bullish' else [],
            sectors_bearish=data.get('sectors', []) if data.get('sentiment') == 'bearish' else [],
            reasoning=data.get('content', '')[:500],
            source='eastmoney_blog'
        )
        
        db.add(viewpoint)
        db.commit()
        
        print(f"[Crawler API] 成功采纳为观点，ID: {viewpoint.id}")
        
        return {
            "success": True,
            "message": "成功采纳为观点",
            "data": {
                "viewpoint_id": viewpoint.id,
                "title": data.get('title', '')[:50]
            }
        }
        
    except Exception as e:
        db.rollback()
        print(f"[Crawler API] 采纳失败：{e}")
        return {
            "success": False,
            "message": f"采纳失败：{e}",
            "data": None
        }


@router.post("/articles/adopt")
def adopt_article(data: dict, db: Session = Depends(get_db)):
    """
    将专业文章采纳为观点
    """
    try:
        print(f"[Crawler API] 采纳专业文章：{data.get('title', '')[:30]}...")
        
        # 创建观点记录
        viewpoint = Viewpoint(
            viewpoint_date=date.today(),
            content=data.get('content', '')[:500],
            author=data.get('author', '编辑'),
            market_direction='neutral',
            confidence=70,
            sectors_bullish=[],
            sectors_bearish=[],
            reasoning=data.get('content', '')[:500],
            source='fund_article'
        )
        
        db.add(viewpoint)
        db.commit()
        
        print(f"[Crawler API] 成功采纳为观点，ID: {viewpoint.id}")
        
        return {
            "success": True,
            "message": "成功采纳为观点",
            "data": {
                "viewpoint_id": viewpoint.id,
                "title": data.get('title', '')[:50]
            }
        }
        
    except Exception as e:
        db.rollback()
        print(f"[Crawler API] 采纳失败：{e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "message": f"采纳失败：{e}",
            "data": None
        }
