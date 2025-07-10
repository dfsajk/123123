from fastapi import FastAPI, APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
import uuid
from datetime import datetime, timedelta
from passlib.context import CryptContext
import jwt
from enum import Enum


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Security setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

# JWT settings
SECRET_KEY = os.environ.get('SECRET_KEY', 'your-secret-key-here-change-in-production')
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Create the main app without a prefix
app = FastAPI(title="School29 Management System", version="1.0.0")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Enums
class UserRole(str, Enum):
    ADMIN = "admin"
    TEACHER = "teacher"
    STUDENT = "student"

class UserStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class NewsStatus(str, Enum):
    PUBLISHED = "published"
    PENDING = "pending"
    REJECTED = "rejected"

# Models
class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: EmailStr
    username: str
    full_name: str
    role: UserRole
    status: UserStatus = UserStatus.PENDING
    class_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = None

class UserCreate(BaseModel):
    email: EmailStr
    username: str
    full_name: str
    password: str
    role: UserRole
    class_id: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    user: User

class Class(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    grade: int
    teacher_id: Optional[str] = None
    student_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ClassCreate(BaseModel):
    name: str
    grade: int
    teacher_id: Optional[str] = None

class News(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    content: str
    author_id: str
    author_name: str
    status: NewsStatus = NewsStatus.PENDING
    views: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    published_at: Optional[datetime] = None

class NewsCreate(BaseModel):
    title: str
    content: str

class Schedule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    class_id: str
    day_of_week: int  # 0-6 (Monday-Sunday)
    time_slot: str  # e.g., "9:00-10:00"
    subject: str
    teacher_id: str
    teacher_name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ScheduleCreate(BaseModel):
    class_id: str
    day_of_week: int
    time_slot: str
    subject: str
    teacher_id: str

class ScheduleChangeRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    schedule_id: str
    teacher_id: str
    teacher_name: str
    requested_changes: dict
    reason: str
    status: str = "pending"  # pending, approved, rejected
    created_at: datetime = Field(default_factory=datetime.utcnow)
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None

class ScheduleChangeRequestCreate(BaseModel):
    schedule_id: str
    requested_changes: dict
    reason: str

class UserActivity(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    action: str
    details: dict
    timestamp: datetime = Field(default_factory=datetime.utcnow)

# Security functions
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception
    
    user = await db.users.find_one({"username": username})
    if user is None:
        raise credentials_exception
    return User(**user)

async def get_current_active_user(current_user: User = Depends(get_current_user)):
    if current_user.status != UserStatus.APPROVED:
        raise HTTPException(status_code=400, detail="User account not approved")
    return current_user

def require_role(required_role: UserRole):
    def role_checker(current_user: User = Depends(get_current_active_user)):
        if current_user.role != required_role and current_user.role != UserRole.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not enough permissions"
            )
        return current_user
    return role_checker

async def log_user_activity(user_id: str, action: str, details: dict = None):
    if details is None:
        details = {}
    activity = UserActivity(
        user_id=user_id,
        action=action,
        details=details
    )
    await db.user_activities.insert_one(activity.dict())

# Authentication routes
@api_router.post("/register", response_model=dict)
async def register(user_data: UserCreate):
    # Check if user already exists
    existing_user = await db.users.find_one({"$or": [{"username": user_data.username}, {"email": user_data.email}]})
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Username or email already registered"
        )
    
    # Hash password
    hashed_password = get_password_hash(user_data.password)
    
    # Create user
    user = User(
        email=user_data.email,
        username=user_data.username,
        full_name=user_data.full_name,
        role=user_data.role,
        class_id=user_data.class_id
    )
    
    # Store user and hashed password
    user_dict = user.dict()
    user_dict["hashed_password"] = hashed_password
    
    await db.users.insert_one(user_dict)
    
    return {"message": "Registration successful. Please wait for admin approval."}

@api_router.post("/login", response_model=Token)
async def login(user_credentials: UserLogin):
    user = await db.users.find_one({"username": user_credentials.username})
    if not user or not verify_password(user_credentials.password, user.get("hashed_password")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if user.get("status") != UserStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail="Account not approved yet"
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]}, expires_delta=access_token_expires
    )
    
    # Update last login
    await db.users.update_one(
        {"username": user_credentials.username},
        {"$set": {"last_login": datetime.utcnow()}}
    )
    
    # Log activity
    await log_user_activity(user["id"], "login")
    
    user_obj = User(**user)
    return {"access_token": access_token, "token_type": "bearer", "user": user_obj}

@api_router.get("/me", response_model=User)
async def read_users_me(current_user: User = Depends(get_current_active_user)):
    return current_user

# Basic route
@api_router.get("/")
async def root():
    return {"message": "School29 Management System API"}

# Public routes (no authentication required)
@api_router.get("/news/public", response_model=List[News])
async def get_public_news():
    """Get published news without authentication"""
    news_list = await db.news.find({"status": NewsStatus.PUBLISHED}).sort("published_at", -1).to_list(100)
    return [News(**news) for news in news_list]

@api_router.get("/news/{news_id}/view")
async def view_news_public(news_id: str):
    """Increment view count for news (public endpoint)"""
    result = await db.news.update_one(
        {"id": news_id, "status": NewsStatus.PUBLISHED},
        {"$inc": {"views": 1}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="News not found")
    
    return {"message": "View counted"}

@api_router.get("/schedule/public", response_model=List[Schedule])
async def get_public_schedule():
    """Get schedule without authentication"""
    schedules = await db.schedules.find().to_list(1000)
    return [Schedule(**schedule) for schedule in schedules]

@api_router.get("/classes/public", response_model=List[Class])
async def get_public_classes():
    """Get classes without authentication"""
    classes = await db.classes.find().to_list(1000)
    return [Class(**class_obj) for class_obj in classes]

# User management routes (Admin only)
@api_router.get("/admin/pending-users", response_model=List[User])
async def get_pending_users(current_user: User = Depends(require_role(UserRole.ADMIN))):
    users = await db.users.find({"status": UserStatus.PENDING}).to_list(1000)
    return [User(**user) for user in users]

@api_router.post("/admin/approve-user/{user_id}")
async def approve_user(user_id: str, current_user: User = Depends(require_role(UserRole.ADMIN))):
    result = await db.users.update_one(
        {"id": user_id},
        {"$set": {"status": UserStatus.APPROVED}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    await log_user_activity(current_user.id, "approve_user", {"approved_user_id": user_id})
    return {"message": "User approved successfully"}

@api_router.post("/admin/reject-user/{user_id}")
async def reject_user(user_id: str, current_user: User = Depends(require_role(UserRole.ADMIN))):
    result = await db.users.update_one(
        {"id": user_id},
        {"$set": {"status": UserStatus.REJECTED}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    await log_user_activity(current_user.id, "reject_user", {"rejected_user_id": user_id})
    return {"message": "User rejected successfully"}

@api_router.get("/admin/users", response_model=List[User])
async def get_all_users(current_user: User = Depends(require_role(UserRole.ADMIN))):
    users = await db.users.find().to_list(1000)
    return [User(**user) for user in users]

# Class management routes
@api_router.post("/classes", response_model=Class)
async def create_class(class_data: ClassCreate, current_user: User = Depends(require_role(UserRole.ADMIN))):
    new_class = Class(**class_data.dict())
    await db.classes.insert_one(new_class.dict())
    await log_user_activity(current_user.id, "create_class", {"class_id": new_class.id})
    return new_class

@api_router.get("/classes", response_model=List[Class])
async def get_classes(current_user: User = Depends(get_current_active_user)):
    classes = await db.classes.find().to_list(1000)
    return [Class(**class_obj) for class_obj in classes]

@api_router.get("/classes/{class_id}", response_model=Class)
async def get_class(class_id: str, current_user: User = Depends(get_current_active_user)):
    class_obj = await db.classes.find_one({"id": class_id})
    if not class_obj:
        raise HTTPException(status_code=404, detail="Class not found")
    return Class(**class_obj)

@api_router.put("/classes/{class_id}", response_model=Class)
async def update_class(class_id: str, class_data: ClassCreate, current_user: User = Depends(require_role(UserRole.ADMIN))):
    result = await db.classes.update_one(
        {"id": class_id},
        {"$set": class_data.dict()}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Class not found")
    
    updated_class = await db.classes.find_one({"id": class_id})
    await log_user_activity(current_user.id, "update_class", {"class_id": class_id})
    return Class(**updated_class)

@api_router.delete("/classes/{class_id}")
async def delete_class(class_id: str, current_user: User = Depends(require_role(UserRole.ADMIN))):
    result = await db.classes.delete_one({"id": class_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Class not found")
    
    await log_user_activity(current_user.id, "delete_class", {"class_id": class_id})
    return {"message": "Class deleted successfully"}

# News management routes
@api_router.post("/news", response_model=News)
async def create_news(news_data: NewsCreate, current_user: User = Depends(get_current_active_user)):
    # Admin can publish directly, others need approval
    status = NewsStatus.PUBLISHED if current_user.role == UserRole.ADMIN else NewsStatus.PENDING
    published_at = datetime.utcnow() if current_user.role == UserRole.ADMIN else None
    
    news = News(
        title=news_data.title,
        content=news_data.content,
        author_id=current_user.id,
        author_name=current_user.full_name,
        status=status,
        published_at=published_at
    )
    
    await db.news.insert_one(news.dict())
    await log_user_activity(current_user.id, "create_news", {"news_id": news.id})
    return news

@api_router.get("/news", response_model=List[News])
async def get_news(current_user: User = Depends(get_current_active_user)):
    # Show only published news for non-admin users
    if current_user.role == UserRole.ADMIN:
        news_list = await db.news.find().to_list(1000)
    else:
        news_list = await db.news.find({"status": NewsStatus.PUBLISHED}).to_list(1000)
    
    return [News(**news) for news in news_list]

@api_router.get("/news/{news_id}", response_model=News)
async def get_news_item(news_id: str, current_user: User = Depends(get_current_active_user)):
    news = await db.news.find_one({"id": news_id})
    if not news:
        raise HTTPException(status_code=404, detail="News not found")
    
    # Increment view count
    await db.news.update_one(
        {"id": news_id},
        {"$inc": {"views": 1}}
    )
    
    # Log view activity
    await log_user_activity(current_user.id, "view_news", {"news_id": news_id})
    
    # Update news object with incremented views
    news["views"] += 1
    return News(**news)

@api_router.get("/admin/pending-news", response_model=List[News])
async def get_pending_news(current_user: User = Depends(require_role(UserRole.ADMIN))):
    news_list = await db.news.find({"status": NewsStatus.PENDING}).to_list(1000)
    return [News(**news) for news in news_list]

@api_router.post("/admin/approve-news/{news_id}")
async def approve_news(news_id: str, current_user: User = Depends(require_role(UserRole.ADMIN))):
    result = await db.news.update_one(
        {"id": news_id},
        {"$set": {"status": NewsStatus.PUBLISHED, "published_at": datetime.utcnow()}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="News not found")
    
    await log_user_activity(current_user.id, "approve_news", {"news_id": news_id})
    return {"message": "News approved successfully"}

@api_router.post("/admin/reject-news/{news_id}")
async def reject_news(news_id: str, current_user: User = Depends(require_role(UserRole.ADMIN))):
    result = await db.news.update_one(
        {"id": news_id},
        {"$set": {"status": NewsStatus.REJECTED}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="News not found")
    
    await log_user_activity(current_user.id, "reject_news", {"news_id": news_id})
    return {"message": "News rejected successfully"}

# Schedule management routes
@api_router.post("/schedule", response_model=Schedule)
async def create_schedule(schedule_data: ScheduleCreate, current_user: User = Depends(require_role(UserRole.ADMIN))):
    # Get teacher name
    teacher = await db.users.find_one({"id": schedule_data.teacher_id})
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    
    schedule = Schedule(
        **schedule_data.dict(),
        teacher_name=teacher["full_name"]
    )
    
    await db.schedules.insert_one(schedule.dict())
    await log_user_activity(current_user.id, "create_schedule", {"schedule_id": schedule.id})
    return schedule

@api_router.get("/schedule", response_model=List[Schedule])
async def get_schedules(current_user: User = Depends(get_current_active_user)):
    schedules = await db.schedules.find().to_list(1000)
    return [Schedule(**schedule) for schedule in schedules]

@api_router.get("/schedule/class/{class_id}", response_model=List[Schedule])
async def get_class_schedule(class_id: str, current_user: User = Depends(get_current_active_user)):
    schedules = await db.schedules.find({"class_id": class_id}).to_list(1000)
    return [Schedule(**schedule) for schedule in schedules]

@api_router.post("/schedule/change-request", response_model=ScheduleChangeRequest)
async def create_schedule_change_request(
    request_data: ScheduleChangeRequestCreate,
    current_user: User = Depends(require_role(UserRole.TEACHER))
):
    # Verify teacher owns the schedule
    schedule = await db.schedules.find_one({"id": request_data.schedule_id})
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    if schedule["teacher_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Not your schedule")
    
    change_request = ScheduleChangeRequest(
        **request_data.dict(),
        teacher_id=current_user.id,
        teacher_name=current_user.full_name
    )
    
    await db.schedule_change_requests.insert_one(change_request.dict())
    await log_user_activity(current_user.id, "create_schedule_change_request", {"request_id": change_request.id})
    return change_request

@api_router.get("/admin/schedule-change-requests", response_model=List[ScheduleChangeRequest])
async def get_pending_schedule_requests(current_user: User = Depends(require_role(UserRole.ADMIN))):
    requests = await db.schedule_change_requests.find({"status": "pending"}).to_list(1000)
    return [ScheduleChangeRequest(**request) for request in requests]

@api_router.post("/admin/approve-schedule-change/{request_id}")
async def approve_schedule_change(request_id: str, current_user: User = Depends(require_role(UserRole.ADMIN))):
    request = await db.schedule_change_requests.find_one({"id": request_id})
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")
    
    # Update the schedule with requested changes
    await db.schedules.update_one(
        {"id": request["schedule_id"]},
        {"$set": request["requested_changes"]}
    )
    
    # Mark request as approved
    await db.schedule_change_requests.update_one(
        {"id": request_id},
        {"$set": {"status": "approved", "reviewed_at": datetime.utcnow(), "reviewed_by": current_user.id}}
    )
    
    await log_user_activity(current_user.id, "approve_schedule_change", {"request_id": request_id})
    return {"message": "Schedule change approved successfully"}

@api_router.post("/admin/reject-schedule-change/{request_id}")
async def reject_schedule_change(request_id: str, current_user: User = Depends(require_role(UserRole.ADMIN))):
    result = await db.schedule_change_requests.update_one(
        {"id": request_id},
        {"$set": {"status": "rejected", "reviewed_at": datetime.utcnow(), "reviewed_by": current_user.id}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Request not found")
    
    await log_user_activity(current_user.id, "reject_schedule_change", {"request_id": request_id})
    return {"message": "Schedule change rejected successfully"}

# Analytics routes
@api_router.get("/admin/analytics")
async def get_analytics(current_user: User = Depends(require_role(UserRole.ADMIN))):
    # Get user statistics
    total_users = await db.users.count_documents({})
    pending_users = await db.users.count_documents({"status": UserStatus.PENDING})
    approved_users = await db.users.count_documents({"status": UserStatus.APPROVED})
    
    # Get news statistics
    total_news = await db.news.count_documents({})
    published_news = await db.news.count_documents({"status": NewsStatus.PUBLISHED})
    pending_news = await db.news.count_documents({"status": NewsStatus.PENDING})
    
    # Get schedule statistics
    total_schedules = await db.schedules.count_documents({})
    pending_schedule_requests = await db.schedule_change_requests.count_documents({"status": "pending"})
    
    # Get activity statistics
    total_activities = await db.user_activities.count_documents({})
    
    # Get most viewed news
    most_viewed_news = await db.news.find({"status": NewsStatus.PUBLISHED}).sort("views", -1).limit(5).to_list(5)
    
    return {
        "users": {
            "total": total_users,
            "pending": pending_users,
            "approved": approved_users
        },
        "news": {
            "total": total_news,
            "published": published_news,
            "pending": pending_news
        },
        "schedule": {
            "total": total_schedules,
            "pending_requests": pending_schedule_requests
        },
        "activities": {
            "total": total_activities
        },
        "most_viewed_news": [News(**news) for news in most_viewed_news]
    }

# Public routes (no authentication required)
@api_router.get("/news/public", response_model=List[News])
async def get_public_news():
    """Get published news without authentication"""
    news_list = await db.news.find({"status": NewsStatus.PUBLISHED}).sort("published_at", -1).to_list(100)
    return [News(**news) for news in news_list]

@api_router.get("/news/{news_id}/view")
async def view_news_public(news_id: str):
    """Increment view count for news (public endpoint)"""
    result = await db.news.update_one(
        {"id": news_id, "status": NewsStatus.PUBLISHED},
        {"$inc": {"views": 1}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="News not found")
    
    return {"message": "View counted"}

@api_router.get("/schedule/public", response_model=List[Schedule])
async def get_public_schedule():
    """Get schedule without authentication"""
    schedules = await db.schedules.find().to_list(1000)
    return [Schedule(**schedule) for schedule in schedules]

@api_router.get("/classes/public", response_model=List[Class])
async def get_public_classes():
    """Get classes without authentication"""
    classes = await db.classes.find().to_list(1000)
    return [Class(**class_obj) for class_obj in classes]

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
