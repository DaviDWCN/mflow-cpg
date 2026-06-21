<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<html>
<head><title>User List</title></head>
<body>
<h1>Users</h1>
<%
    String title = "User Management";
    int count = 0;
%>
<h2><%= title %></h2>
<ul>
<%
    for (int i = 0; i < 10; i++) {
        count = count + 1;
    }
%>
</ul>
<p>Total: <%= count %></p>
</body>
</html>
