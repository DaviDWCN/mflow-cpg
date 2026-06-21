package com.example.web;

import org.apache.struts.action.Action;
import org.apache.struts.action.ActionForm;
import org.apache.struts.action.ActionForward;
import org.apache.struts.action.ActionMapping;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

public class LoginAction extends Action {

    public ActionForward execute(ActionMapping mapping,
                                 ActionForm form,
                                 HttpServletRequest request,
                                 HttpServletResponse response) {
        String username = request.getParameter("username");
        String password = request.getParameter("password");

        if (username != null && password != null) {
            boolean valid = authenticate(username, password);
            if (valid) {
                return mapping.findForward("success");
            }
        }
        return mapping.findForward("failure");
    }

    private boolean authenticate(String username, String password) {
        return username.equals("admin") && password.equals("secret");
    }
}
