"""
基础服务类
提供通用的 CRUD 操作
"""
from typing import Generic, TypeVar, Optional, List, Type
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger(__name__)

ModelType = TypeVar("ModelType")


class BaseService(Generic[ModelType]):
    """
    基础服务类
    
    提供通用的数据库 CRUD 操作，所有服务类继承此类
    """
    
    def __init__(self, db: Session, model: Type[ModelType]):
        """
        初始化服务
        
        Args:
            db: 数据库会话
            model: ORM 模型类
        """
        self.db = db
        self.model = model
    
    def get(self, id: int) -> Optional[ModelType]:
        """
        根据 ID 获取单个记录
        
        Args:
            id: 记录 ID
            
        Returns:
            模型实例或 None
        """
        return self.db.query(self.model).filter(self.model.id == id).first()
    
    def get_all(self, skip: int = 0, limit: int = 100) -> List[ModelType]:
        """
        获取所有记录（分页）
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            模型实例列表
        """
        return self.db.query(self.model).offset(skip).limit(limit).all()
    
    def create(self, obj_in: dict) -> ModelType:
        """
        创建新记录

        Args:
            obj_in: 创建数据字典

        Returns:
            创建的模型实例

        Raises:
            sqlalchemy.exc.IntegrityError: 唯一键冲突或外键约束失败
            Exception: 其他数据库错误
        """
        db_obj = self.model(**obj_in)
        self.db.add(db_obj)
        try:
            self.db.commit()
            self.db.refresh(db_obj)
        except Exception as e:
            self.db.rollback()
            logger.error(f"创建记录失败: {e}", exc_info=True)
            raise e
        return db_obj
    
    def update(self, id: int, obj_in: dict) -> Optional[ModelType]:
        """
        更新记录

        Args:
            id: 记录 ID
            obj_in: 更新数据字典

        Returns:
            更新后的模型实例或 None

        Raises:
            sqlalchemy.exc.IntegrityError: 唯一键冲突或外键约束失败
            Exception: 其他数据库错误
        """
        db_obj = self.get(id)
        if db_obj:
            for key, value in obj_in.items():
                if hasattr(db_obj, key):
                    setattr(db_obj, key, value)
            try:
                self.db.commit()
                self.db.refresh(db_obj)
            except Exception as e:
                self.db.rollback()
                logger.error(f"更新记录 {id} 失败: {e}", exc_info=True)
                raise e
        return db_obj
    
    def delete(self, id: int) -> bool:
        """
        删除记录

        Args:
            id: 记录 ID

        Returns:
            是否删除成功

        Note:
            如果删除失败（如外键约束冲突），会自动回滚并返回 False
        """
        db_obj = self.get(id)
        if db_obj:
            try:
                self.db.delete(db_obj)
                self.db.commit()
                return True
            except Exception as e:
                self.db.rollback()
                logger.error(f"删除记录 {id} 失败: {e}", exc_info=True)
                return False
        return False
    
    def count(self) -> int:
        """
        获取记录总数

        Returns:
            记录数量，查询失败时返回 0
        """
        try:
            return self.db.query(self.model).count()
        except Exception as e:
            self.db.rollback()
            logger.error(f"获取记录数失败: {e}", exc_info=True)
            return 0
    
    def exists(self, id: int) -> bool:
        """
        检查记录是否存在
        
        Args:
            id: 记录 ID
            
        Returns:
            是否存在
        """
        return self.get(id) is not None
