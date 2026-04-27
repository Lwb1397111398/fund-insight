"""
爬虫服务
处理爬虫相关的业务逻辑
"""
from typing import List, Dict, Optional, Tuple
from datetime import date, timedelta
from sqlalchemy.orm import Session
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time

from src.models.database import Viewpoint
from src.core.config import config


class CrawlerService:
    """爬虫服务类"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def _is_duplicate_title(self, title: str, similarity_threshold: float = 0.8) -> bool:
        """
        检查标题是否重复（基于相似度）
        
        Args:
            title: 待检查的标题
            similarity_threshold: 相似度阈值
            
        Returns:
            是否重复
        """
        from datetime import date, timedelta
        
        recent_date = date.today() - timedelta(days=7)
        
        recent_viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= recent_date,
            Viewpoint.is_deleted == False
        ).all()
        
        title_lower = title.lower().strip()
        title_words = set(title_lower)
        
        for vp in recent_viewpoints:
            if not vp.content:
                continue
            
            vp_title = vp.content[:100].lower().strip() if len(vp.content) > 50 else vp.content.lower().strip()
            
            if title_lower == vp_title:
                return True
            
            vp_words = set(vp_title)
            if title_words and vp_words:
                intersection = len(title_words & vp_words)
                union = len(title_words | vp_words)
                similarity = intersection / union if union > 0 else 0
                
                if similarity >= similarity_threshold:
                    return True
        
        return False
    
    def is_crawler_enabled(self) -> bool:
        """
        检查爬虫是否启用
        
        Returns:
            是否启用
        """
        return config.CRAWLER_ENABLED
    
    def get_crawler_status(self) -> Dict:
        """
        获取爬虫模块状态
        
        Returns:
            爬虫状态字典
        """
        return {
            "enabled": self.is_crawler_enabled(),
            "modules": {
                "eastmoney_blog": True,
                "eastmoney_guide": True,
                "sina_finance": True,
                "sina_blog": True
            }
        }
    
    def _analyze_article(self, ai_analyzer, title: str, content: str, source: str = "manual") -> Tuple[bool, Dict]:
        """
        分析文章是否应该被采纳
        
        Args:
            ai_analyzer: AI分析器
            title: 文章标题
            content: 文章内容
            source: 来源标识
            
        Returns:
            (是否应该采纳, 分析结果)
        """
        # 先检查标题是否重复
        if self._is_duplicate_title(title):
            return False, {
                'score': 0,
                'reason': '标题与已有观点重复',
                'should_capture': False,
                'threshold': 0
            }
        
        ai_result = ai_analyzer.should_capture({
            'title': title,
            'content': content,
        }, source=source)
        
        threshold = self._get_threshold(source)
        should_capture = ai_result.should_capture and ai_result.score >= threshold
        
        if should_capture:
            analysis = ai_analyzer.analyze_post_simple({
                'title': title,
                'content': content
            })
            return True, {
                'score': ai_result.score,
                'reason': ai_result.reason,
                'sentiment': analysis.get('sentiment', 'neutral'),
                'sentiment_score': analysis.get('sentiment_score', 0),
                'sectors': analysis.get('sectors', [])
            }
        
        return False, {
            'score': ai_result.score,
            'reason': ai_result.reason,
            'should_capture': ai_result.should_capture,
            'threshold': threshold
        }
    
    def _get_threshold(self, source: str) -> float:
        """获取抓取阈值"""
        return 7.5

    def _analyze_articles_concurrent(
        self,
        articles: List[Dict],
        source: str,
        max_workers: int = 3
    ) -> List[Tuple[Dict, bool, Dict]]:
        """
        并发分析文章列表

        Args:
            articles: 文章列表
            source: 来源标识
            max_workers: 最大并发数

        Returns:
            分析结果列表，每项为 (article, should_capture, analysis)
        """
        from src.crawler.enhanced_ai_analyzer import EnhancedAIAnalyzer
        from src.models.database import SessionLocal

        results = []
        results_lock = threading.Lock()

        def analyze_single(article: Dict) -> Tuple[Dict, bool, Dict]:
            """分析单篇文章"""
            try:
                db = SessionLocal()
                try:
                    recent_date = date.today() - timedelta(days=7)
                    recent_viewpoints = db.query(Viewpoint).filter(
                        Viewpoint.viewpoint_date >= recent_date,
                        Viewpoint.is_deleted == False
                    ).all()
                    title_lower = article['title'].lower().strip()
                    title_words = set(title_lower)
                    is_dup = False
                    for vp in recent_viewpoints:
                        if not vp.content:
                            continue
                        vp_title = vp.content[:100].lower().strip() if len(vp.content) > 50 else vp.content.lower().strip()
                        if title_lower == vp_title:
                            is_dup = True
                            break
                        vp_words = set(vp_title)
                        if title_words and vp_words:
                            similarity = len(title_words & vp_words) / len(title_words | vp_words) if len(title_words | vp_words) > 0 else 0
                            if similarity >= 0.8:
                                is_dup = True
                                break
                    
                    if is_dup:
                        return article, False, {
                            'score': 0,
                            'reason': '标题与已有观点重复',
                            'should_capture': False,
                            'threshold': 0
                        }

                    # 创建独立的AI分析器
                    ai_analyzer = EnhancedAIAnalyzer()
                    ai_result = ai_analyzer.should_capture({
                        'title': article['title'],
                        'content': article.get('content', ''),
                    }, source=source)

                    threshold = self._get_threshold(source)
                    should_capture = ai_result.should_capture and ai_result.score >= threshold

                    if should_capture:
                        analysis = ai_analyzer.analyze_post_simple({
                            'title': article['title'],
                            'content': article.get('content', '')
                        })
                        return article, True, {
                            'score': ai_result.score,
                            'reason': ai_result.reason,
                            'sentiment': analysis.get('sentiment', 'neutral'),
                            'sentiment_score': analysis.get('sentiment_score', 0),
                            'sectors': analysis.get('sectors', [])
                        }
                    else:
                        return article, False, {
                            'score': ai_result.score,
                            'reason': ai_result.reason,
                            'should_capture': ai_result.should_capture,
                            'threshold': threshold
                        }
                finally:
                    db.close()
            except Exception as e:
                print(f"[并发分析] 分析文章失败: {article.get('title', '')[:30]}... 错误: {e}")
                return article, False, {'score': 0, 'reason': f'分析失败: {e}', 'should_capture': False}

        print(f"[并发分析] 开始并发分析 {len(articles)} 篇文章，并发数: {max_workers}")
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_article = {
                executor.submit(analyze_single, article): article
                for article in articles
            }

            completed = 0
            for future in as_completed(future_to_article):
                article, should_capture, analysis = future.result()
                with results_lock:
                    results.append((article, should_capture, analysis))
                    completed += 1
                    if completed % 5 == 0 or completed == len(articles):
                        print(f"[并发分析] 进度: {completed}/{len(articles)}")

        elapsed = time.time() - start_time
        print(f"[并发分析] 完成，耗时: {elapsed:.1f}秒")

        return results

    def _create_viewpoint(
        self,
        content: str,
        title: str,
        author: str,
        source: str,
        analysis: Dict
    ) -> Viewpoint:
        """
        创建观点
        
        Args:
            content: 内容
            title: 标题
            author: 作者
            source: 来源
            analysis: 分析结果
            
        Returns:
            创建的观点
        """
        sentiment = analysis.get('sentiment', 'neutral')
        sectors = analysis.get('sectors', [])
        
        viewpoint = Viewpoint(
            viewpoint_date=date.today(),
            content=content if content else title,
            author=author,
            market_direction=sentiment,
            confidence=int(abs(analysis.get('sentiment_score', 0)) * 100),
            sectors_bullish=sectors if sentiment == 'bullish' else [],
            sectors_bearish=sectors if sentiment == 'bearish' else [],
            reasoning=analysis.get('reason', ''),
            source=source
        )
        
        return viewpoint
    
    def crawl_eastmoney_blog(self, max_articles: int = 15, concurrent: bool = True, max_workers: int = 3) -> Dict:
        """
        抓取东方财富博客

        Args:
            max_articles: 最大文章数
            concurrent: 是否使用并发分析
            max_workers: 并发数

        Returns:
            抓取结果
        """
        if not self.is_crawler_enabled():
            return {"success": False, "message": "爬虫模块未启用"}

        print(f"\n{'='*60}")
        print(f"开始抓取东方财富博客 (最多 {max_articles} 篇, 并发: {concurrent}, 并发数: {max_workers})")
        print(f"{'='*60}")

        try:
            from src.crawler.eastmoney_blog_crawler import crawler as eastmoney_crawler

            articles = eastmoney_crawler.fetch_hot_articles(max_articles=max_articles)
            print(f"抓取到 {len(articles)} 篇文章，开始分析...\n")

            adopted_count = 0
            skipped_count = 0
            adopted_articles = []
            skipped_articles = []

            if concurrent and len(articles) > 1:
                # 使用并发分析
                results = self._analyze_articles_concurrent(articles, 'eastmoney_blog', max_workers)

                for article, should_capture, analysis in results:
                    score = analysis.get('score', 0)
                    threshold = self._get_threshold('eastmoney_blog')
                    reason = analysis.get('reason', '')[:30]

                    if should_capture:
                        viewpoint = self._create_viewpoint(
                            content=article['content'],
                            title=article['title'],
                            author=article['author'],
                            source='eastmoney_blog',
                            analysis=analysis
                        )

                        self.db.add(viewpoint)
                        adopted_count += 1
                        adopted_articles.append({
                            'title': article['title'],
                            'author': article['author'],
                            'score': score,
                            'sentiment': analysis.get('sentiment')
                        })
                    else:
                        skipped_count += 1
                        skipped_articles.append({
                            'title': article['title'],
                            'score': score,
                            'reason': reason
                        })
            else:
                # 串行分析
                from src.crawler.enhanced_ai_analyzer import EnhancedAIAnalyzer
                ai_analyzer = EnhancedAIAnalyzer()

                for i, article in enumerate(articles, 1):
                    should_capture, analysis = self._analyze_article(
                        ai_analyzer,
                        article['title'],
                        article['content'],
                        source='eastmoney_blog'
                    )

                    score = analysis.get('score', 0)
                    threshold = self._get_threshold('eastmoney_blog')
                    reason = analysis.get('reason', '')[:30]

                    print(f"[{i}/{len(articles)}] {'✅ 采纳' if should_capture else '❌ 跳过'} | 分数: {score:.1f}/{threshold} | {article['title'][:40]}...")

                    if should_capture:
                        viewpoint = self._create_viewpoint(
                            content=article['content'],
                            title=article['title'],
                            author=article['author'],
                            source='eastmoney_blog',
                            analysis=analysis
                        )

                        self.db.add(viewpoint)
                        adopted_count += 1
                        adopted_articles.append({
                            'title': article['title'],
                            'author': article['author'],
                            'score': score,
                            'sentiment': analysis.get('sentiment')
                        })
                    else:
                        skipped_count += 1
                        skipped_articles.append({
                            'title': article['title'],
                            'score': score,
                            'reason': reason
                        })

            self.db.commit()

            print(f"\n{'='*60}")
            print(f"东方财富博客抓取完成")
            print(f"✅ 采纳: {adopted_count} 篇")
            print(f"❌ 跳过: {skipped_count} 篇")
            print(f"{'='*60}\n")

            return {
                "success": True,
                "message": f"自动采纳完成：采纳 {adopted_count} 篇，跳过 {skipped_count} 篇",
                "data": {
                    "adopted_count": adopted_count,
                    "skipped_count": skipped_count,
                    "articles": adopted_articles,
                    "skipped": skipped_articles
                }
            }

        except Exception as e:
            self.db.rollback()
            raise e
    
    def crawl_eastmoney_guide(self, max_articles: int = 20, concurrent: bool = True, max_workers: int = 3) -> Dict:
        """
        抓取东财博客导读

        Args:
            max_articles: 最大文章数
            concurrent: 是否使用并发分析
            max_workers: 并发数

        Returns:
            抓取结果
        """
        if not self.is_crawler_enabled():
            return {"success": False, "message": "爬虫模块未启用"}

        print(f"\n{'='*60}")
        print(f"开始抓取东方财富指南 (最多 {max_articles} 篇, 并发: {concurrent}, 并发数: {max_workers})")
        print(f"{'='*60}")

        try:
            from src.crawler.eastmoney_guide_crawler import get_guide_crawler

            crawler = get_guide_crawler()
            articles = crawler.fetch_guide_articles(max_articles=max_articles)
            print(f"抓取到 {len(articles)} 篇文章，开始分析...\n")

            adopted_count = 0
            skipped_count = 0
            adopted_articles = []
            skipped_articles = []

            if concurrent and len(articles) > 1:
                results = self._analyze_articles_concurrent(articles, 'eastmoney_guide', max_workers)

                for article, should_capture, analysis in results:
                    score = analysis.get('score', 0)
                    threshold = self._get_threshold('eastmoney_guide')

                    if should_capture:
                        viewpoint = self._create_viewpoint(
                            content=article.get('content', ''),
                            title=article['title'],
                            author=article.get('author', '东财博客'),
                            source='eastmoney_guide',
                            analysis=analysis
                        )

                        self.db.add(viewpoint)
                        adopted_count += 1
                        adopted_articles.append({
                            'title': article['title'],
                            'author': article.get('author'),
                            'score': score,
                            'sentiment': analysis.get('sentiment')
                        })
                    else:
                        skipped_count += 1
                        skipped_articles.append({
                            'title': article['title'],
                            'score': score,
                            'reason': analysis.get('reason', '')[:30]
                        })
            else:
                from src.crawler.enhanced_ai_analyzer import EnhancedAIAnalyzer
                ai_analyzer = EnhancedAIAnalyzer()

                for i, article in enumerate(articles, 1):
                    should_capture, analysis = self._analyze_article(
                        ai_analyzer,
                        article['title'],
                        article.get('content', ''),
                        source='eastmoney_guide'
                    )

                    score = analysis.get('score', 0)
                    threshold = self._get_threshold('eastmoney_guide')

                    print(f"[{i}/{len(articles)}] {'✅ 采纳' if should_capture else '❌ 跳过'} | 分数: {score:.1f}/{threshold} | {article['title'][:40]}...")

                    if should_capture:
                        viewpoint = self._create_viewpoint(
                            content=article.get('content', ''),
                            title=article['title'],
                            author=article.get('author', '东财博客'),
                            source='eastmoney_guide',
                            analysis=analysis
                        )

                        self.db.add(viewpoint)
                        adopted_count += 1
                        adopted_articles.append({
                            'title': article['title'],
                            'author': article.get('author'),
                            'score': score,
                            'sentiment': analysis.get('sentiment')
                        })
                    else:
                        skipped_count += 1
                        skipped_articles.append({
                            'title': article['title'],
                            'score': score,
                            'reason': analysis.get('reason', '')[:30]
                        })

            self.db.commit()

            print(f"\n{'='*60}")
            print(f"东方财富指南抓取完成")
            print(f"✅ 采纳: {adopted_count} 篇")
            print(f"❌ 跳过: {skipped_count} 篇")
            print(f"{'='*60}\n")

            return {
                "success": True,
                "message": f"自动采纳完成：采纳 {adopted_count} 篇，跳过 {skipped_count} 篇",
                "data": {
                    "adopted_count": adopted_count,
                    "skipped_count": skipped_count,
                    "articles": adopted_articles,
                    "skipped": skipped_articles
                }
            }

        except Exception as e:
            self.db.rollback()
            raise e
    
    def crawl_sina_finance(self, category: str = 'finance', max_articles: int = 20, concurrent: bool = True, max_workers: int = 3) -> Dict:
        """
        抓取新浪财经

        Args:
            category: 分类
            max_articles: 最大文章数
            concurrent: 是否使用并发分析
            max_workers: 并发数

        Returns:
            抓取结果
        """
        if not self.is_crawler_enabled():
            return {"success": False, "message": "爬虫模块未启用"}

        print(f"\n{'='*60}")
        print(f"开始抓取新浪财经 (最多 {max_articles} 篇, 并发: {concurrent}, 并发数: {max_workers})")
        print(f"{'='*60}")

        try:
            from src.crawler.sina_finance_crawler import get_sina_crawler

            crawler = get_sina_crawler()
            articles = crawler.fetch_articles(category=category, num=max_articles)
            print(f"抓取到 {len(articles)} 篇文章，开始分析...\n")

            adopted_count = 0
            skipped_count = 0
            adopted_articles = []
            skipped_articles = []

            if concurrent and len(articles) > 1:
                results = self._analyze_articles_concurrent(articles, 'sina_finance', max_workers)

                for article, should_capture, analysis in results:
                    score = analysis.get('score', 0)
                    threshold = self._get_threshold('sina_finance')

                    if should_capture:
                        viewpoint = self._create_viewpoint(
                            content=article.get('content', ''),
                            title=article['title'],
                            author=article.get('author', '新浪财经'),
                            source='sina_finance',
                            analysis=analysis
                        )

                        self.db.add(viewpoint)
                        adopted_count += 1
                        adopted_articles.append({
                            'title': article['title'],
                            'author': article.get('author'),
                            'score': score,
                            'sentiment': analysis.get('sentiment')
                        })
                    else:
                        skipped_count += 1
                        skipped_articles.append({
                            'title': article['title'],
                            'score': score,
                            'reason': analysis.get('reason', '')[:30]
                        })
            else:
                from src.crawler.enhanced_ai_analyzer import EnhancedAIAnalyzer
                ai_analyzer = EnhancedAIAnalyzer()

                for i, article in enumerate(articles, 1):
                    should_capture, analysis = self._analyze_article(
                        ai_analyzer,
                        article['title'],
                        article.get('content', ''),
                        source='sina_finance'
                    )

                    score = analysis.get('score', 0)
                    threshold = self._get_threshold('sina_finance')

                    print(f"[{i}/{len(articles)}] {'✅ 采纳' if should_capture else '❌ 跳过'} | 分数: {score:.1f}/{threshold} | {article['title'][:40]}...")

                    if should_capture:
                        viewpoint = self._create_viewpoint(
                            content=article.get('content', ''),
                            title=article['title'],
                            author=article.get('author', '新浪财经'),
                            source='sina_finance',
                            analysis=analysis
                        )

                        self.db.add(viewpoint)
                        adopted_count += 1
                        adopted_articles.append({
                            'title': article['title'],
                            'author': article.get('author'),
                            'score': score,
                            'sentiment': analysis.get('sentiment')
                        })
                    else:
                        skipped_count += 1
                        skipped_articles.append({
                            'title': article['title'],
                            'score': score,
                            'reason': analysis.get('reason', '')[:30]
                        })

            self.db.commit()

            print(f"\n{'='*60}")
            print(f"新浪财经抓取完成")
            print(f"✅ 采纳: {adopted_count} 篇")
            print(f"❌ 跳过: {skipped_count} 篇")
            print(f"{'='*60}\n")

            return {
                "success": True,
                "message": f"自动采纳完成：采纳 {adopted_count} 篇，跳过 {skipped_count} 篇",
                "data": {
                    "adopted_count": adopted_count,
                    "skipped_count": skipped_count,
                    "articles": adopted_articles,
                    "skipped": skipped_articles
                }
            }

        except Exception as e:
            self.db.rollback()
            raise e
    
    def crawl_sina_blog(self, max_posts: int = 20, concurrent: bool = True, max_workers: int = 3) -> Dict:
        """
        抓取新浪博客

        Args:
            max_posts: 最大文章数
            concurrent: 是否使用并发分析
            max_workers: 并发数

        Returns:
            抓取结果
        """
        if not self.is_crawler_enabled():
            return {"success": False, "message": "爬虫模块未启用"}

        print(f"\n{'='*60}")
        print(f"开始抓取新浪博客 (最多 {max_posts} 篇, 并发: {concurrent}, 并发数: {max_workers})")
        print(f"{'='*60}")

        try:
            from src.crawler.sina_blog_crawler import get_blog_crawler

            crawler = get_blog_crawler()
            articles = crawler.fetch_blog_posts(max_posts=max_posts)
            print(f"抓取到 {len(articles)} 篇文章，开始分析...\n")

            adopted_count = 0
            skipped_count = 0
            adopted_articles = []
            skipped_articles = []

            if concurrent and len(articles) > 1:
                results = self._analyze_articles_concurrent(articles, 'sina_blog', max_workers)

                for article, should_capture, analysis in results:
                    score = analysis.get('score', 0)
                    threshold = self._get_threshold('sina_blog')

                    if should_capture:
                        viewpoint = self._create_viewpoint(
                            content=article.get('content', ''),
                            title=article['title'],
                            author=article.get('author', '新浪博客'),
                            source='sina_blog',
                            analysis=analysis
                        )

                        self.db.add(viewpoint)
                        adopted_count += 1
                        adopted_articles.append({
                            'title': article['title'],
                            'author': article.get('author'),
                            'score': score,
                            'sentiment': analysis.get('sentiment')
                        })
                    else:
                        skipped_count += 1
                        skipped_articles.append({
                            'title': article['title'],
                            'score': score,
                            'reason': analysis.get('reason', '')[:30]
                        })
            else:
                from src.crawler.enhanced_ai_analyzer import EnhancedAIAnalyzer
                ai_analyzer = EnhancedAIAnalyzer()

                for i, article in enumerate(articles, 1):
                    should_capture, analysis = self._analyze_article(
                        ai_analyzer,
                        article['title'],
                        article.get('content', ''),
                        source='sina_blog'
                    )

                    score = analysis.get('score', 0)
                    threshold = self._get_threshold('sina_blog')

                    print(f"[{i}/{len(articles)}] {'✅ 采纳' if should_capture else '❌ 跳过'} | 分数: {score:.1f}/{threshold} | {article['title'][:40]}...")

                    if should_capture:
                        viewpoint = self._create_viewpoint(
                            content=article.get('content', ''),
                            title=article['title'],
                            author=article.get('author', '新浪博客'),
                            source='sina_blog',
                            analysis=analysis
                        )

                        self.db.add(viewpoint)
                        adopted_count += 1
                        adopted_articles.append({
                            'title': article['title'],
                            'author': article.get('author'),
                            'score': score,
                            'sentiment': analysis.get('sentiment')
                        })
                    else:
                        skipped_count += 1
                        skipped_articles.append({
                            'title': article['title'],
                            'score': score,
                            'reason': analysis.get('reason', '')[:30]
                        })

            self.db.commit()

            print(f"\n{'='*60}")
            print(f"新浪博客抓取完成")
            print(f"✅ 采纳: {adopted_count} 篇")
            print(f"❌ 跳过: {skipped_count} 篇")
            print(f"{'='*60}\n")

            return {
                "success": True,
                "message": f"自动采纳完成：采纳 {adopted_count} 篇，跳过 {skipped_count} 篇",
                "data": {
                    "adopted_count": adopted_count,
                    "skipped_count": skipped_count,
                    "articles": adopted_articles,
                    "skipped": skipped_articles
                }
            }

        except Exception as e:
            self.db.rollback()
            raise e