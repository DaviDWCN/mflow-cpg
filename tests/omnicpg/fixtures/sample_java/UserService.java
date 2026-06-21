package com.example.service;

import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;

@Service
public class UserService {

    @Autowired
    private UserRepository userRepository;

    private String defaultGreeting;

    public UserService() {
        defaultGreeting = "Hello";
    }

    public String greet(String name) {
        String message = defaultGreeting + ", " + name;
        System.out.println(message);
        return message;
    }

    public void processUser(String userId) {
        if (userId != null) {
            String user = userRepository.findById(userId);
            greet(user);
        } else {
            greet("stranger");
        }
    }

    public void batchProcess(String[] ids) {
        for (int i = 0; i < ids.length; i++) {
            processUser(ids[i]);
        }
    }

    public String safeOperation(String input) {
        String result;
        try {
            result = doRiskyOperation(input);
        } catch (Exception e) {
            result = "error";
            e.printStackTrace();
        } finally {
            System.out.println("done");
        }
        return result;
    }

    private String doRiskyOperation(String input) {
        return input.toUpperCase();
    }
}
