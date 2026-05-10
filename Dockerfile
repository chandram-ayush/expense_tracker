# 1. Use an official, lightweight Python image
FROM python:3.10-slim

# 2. Set the working directory inside the container
WORKDIR /app

# 3. Copy the requirements file first (this helps Docker cache your installations)
COPY requirements.txt .

# 4. Install all the required Python libraries
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy all your remaining project files into the container
COPY . .

# 6. Tell Docker that Streamlit uses port 8501
EXPOSE 8501

# 7. The command to start the Streamlit dashboard
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]