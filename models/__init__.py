"""集中导入所有模型，保证注册到 Base.metadata，迁移才不会漏表"""
from models.index_job import IndexJob
from models.knowledge_audit_log import KnowledgeAuditLog
from models.ops_audit_log import OpsAuditLog
from models.knowledge_doc import KnowledgeDoc
from models.knowledge_chunk import KnowledgeChunk
from models.graph_entity import GraphEntity
from models.graph_relation import GraphRelation
from models.graph_job import GraphJob
from models.chat_session import ChatSession
from models.chat_message import ChatMessage
from models.base import Base
from models.chunk import UploadChunk, UploadTask
from models.file import FileInfo
from models.download_log import DownloadLog
from models.user import User
from models.employee import Employee